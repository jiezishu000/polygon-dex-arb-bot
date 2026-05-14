"""
暗潮引擎 · Polygon DEX 套利引擎 v2
黑灰暗帝国 · 溟

扫描 QuickSwap / SushiSwap 多个交易对的价差机会。
支持 USDC 和 USDT 双稳定币。
需要私钥签名交易，当前为 DRY RUN 模式。

运行:
  python polygon_swapper.py scan        扫描所有交易对价差
  python polygon_swapper.py monitor     持续监控 + 自动交易
  python polygon_swapper.py trade       执行最佳套利
  python polygon_swapper.py status      钱包/网络状态
  python polygon_swapper.py balances    全链钱包总览

依赖:
  pip install web3 requests
"""

import sys, io, os, json, time
from datetime import datetime
from typing import Optional

if __name__ == "__main__" and hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import requests
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

# ── 网络 ──
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"

# ── DEX Routers ──
QUICKSWAP_ROUTER = "0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff"
SUSHISWAP_ROUTER = "0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506"

# ── 代币 (Polygon) ──
WMATIC = "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270"
USDC   = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # 6 decimals
USDT   = "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"  # 6 decimals
WETH   = "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619"  # 18 decimals

# ── DEX Factory 地址 (用于查 pair) ──
FACTORIES = {
    "QuickSwap": "0x5757371414417b8C6CAad45bAeF941aBc7d3Ab32",
    "SushiSwap": "0xc35DADB65012eC5796536bD9864eD8773aBc74C4",
}

# ── 扫描的交易对 ──
# (tokenA, tokenB, 显示名)
SCAN_PAIRS = [
    (WMATIC, USDC, "MATIC/USDC"),
    (WMATIC, USDT, "MATIC/USDT"),
    (WETH,   USDC, "WETH/USDC"),
    (WETH,   USDT, "WETH/USDT"),
]

# ── 阈值 ──
MIN_PROFIT_USD = 0.3
SLIPPAGE = 0.01

# ── 合约 ABI ──
ROUTER_ABI = [
    {"inputs":[{"name":"amountIn","type":"uint256"},{"name":"path","type":"address[]"}],"name":"getAmountsOut","outputs":[{"name":"amounts","type":"uint256[]"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"amountIn","type":"uint256"},{"name":"amountOutMin","type":"uint256"},{"name":"path","type":"address[]"},{"name":"to","type":"address"},{"name":"deadline","type":"uint256"}],"name":"swapExactTokensForTokens","outputs":[{"name":"amounts","type":"uint256[]"}],"stateMutability":"nonpayable","type":"function"},
]
ERC20_ABI = [
    {"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"stateMutability":"view","type":"function"},
]
PAIR_ABI = [
    {"inputs":[],"name":"getReserves","outputs":[{"name":"_reserve0","type":"uint112"},{"name":"_reserve1","type":"uint112"},{"name":"_blockTimestampLast","type":"uint32"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"token0","outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"},
]


# ══════════════════════════════════════════════════════════════
#  全局 Web3 实例
# ══════════════════════════════════════════════════════════════

_w3: Optional[Web3] = None

def get_w3() -> Optional[Web3]:
    global _w3
    if _w3 is None:
        try:
            w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
            if w3.is_connected():
                w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                _w3 = w3
        except Exception:
            pass
    return _w3


def net_gas_price() -> int:
    """当前 gas price (wei)"""
    w3 = get_w3()
    if w3:
        try:
            return w3.eth.gas_price
        except Exception:
            pass
    # fallback via RPC
    r = requests.post(POLYGON_RPC, json={"jsonrpc":"2.0","id":1,"method":"eth_gasPrice","params":[]}, timeout=5).json()
    return int(r.get("result","0x0"), 16)


def net_block() -> int:
    w3 = get_w3()
    if w3:
        try: return w3.eth.block_number
        except: pass
    return 0


def net_chain_id() -> int:
    w3 = get_w3()
    if w3:
        try: return w3.eth.chain_id
        except: pass
    r = requests.post(POLYGON_RPC, json={"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}, timeout=5).json()
    return int(r.get("result","0x0"), 16)


def get_pair_address(factory: str, token_a: str, token_b: str) -> Optional[str]:
    """通过 DEX Factory 获取 pair 合约地址"""
    w3 = get_w3()
    if not w3:
        return None
    # getPair selector: 0xe6a43905
    sel = "0xe6a43905"
    data = sel + "000000000000000000000000" + token_a[2:].lower() + "000000000000000000000000" + token_b[2:].lower()
    r = requests.post(POLYGON_RPC, json={"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to": factory, "data": data}, "latest"]}, timeout=5).json()
    result = r.get("result", "0x")
    if result and result != "0x" and len(result) >= 66:
        addr = "0x" + result[-40:]
        if addr != "0x0000000000000000000000000000000000000000":
            return addr
    return None


# ══════════════════════════════════════════════════════════════
#  Pool 价格引擎 (纯 Web3)
# ══════════════════════════════════════════════════════════════

class PoolSnapshot:
    """单个 DEX 交易对的实时快照"""

    def __init__(self, dex: str, token_a: str, token_b: str, label: str, pair_addr: str):
        self.dex = dex
        self.token_a = token_a
        self.token_b = token_b
        self.label = label
        self.pair_addr = pair_addr
        self.reserve_a: int = 0
        self.reserve_b: int = 0
        self.price_a: float = 0.0   # 1 token_a = X token_b
        self.price_b: float = 0.0   # 1 token_b = X token_a
        self.liquidity_usd: float = 0.0
        self.ok = False

    def fetch(self) -> bool:
        w3 = get_w3()
        if not w3:
            return False
        try:
            pair = w3.to_checksum_address(self.pair_addr)
            contract = w3.eth.contract(address=pair, abi=PAIR_ABI)
            token0_addr = contract.functions.token0().call()
            reserves = contract.functions.getReserves()
            reserve0, reserve1, _ = reserves.call()

            # 确定 token_a 是 token0 还是 token1
            if token0_addr.lower() == self.token_a.lower():
                self.reserve_a, self.reserve_b = reserve0, reserve1
            else:
                self.reserve_a, self.reserve_b = reserve1, reserve0

            if self.reserve_a == 0 or self.reserve_b == 0:
                return False

            # 获取精度
            def decimals_of(addr: str) -> int:
                c = w3.eth.contract(address=w3.to_checksum_address(addr), abi=ERC20_ABI)
                return c.functions.decimals().call()

            try:
                dec_a = decimals_of(self.token_a)
                dec_b = decimals_of(self.token_b)
            except Exception:
                return False

            ra = self.reserve_a / (10 ** dec_a)
            rb = self.reserve_b / (10 ** dec_b)
            self.price_a = rb / ra  # 1 token_a = X token_b
            self.price_b = ra / rb  # 1 token_b = X token_a

            # 流动性 ≈ 稳定币侧 * 2
            if "USDC" in self.label or "USDT" in self.label:
                self.liquidity_usd = rb * 2
            else:
                self.liquidity_usd = ra * 2 * self.price_a

            self.ok = True
            return True
        except Exception:
            return False

    def price_in_usd(self, matic_price: float) -> float:
        """估算以 USD 计价的价格"""
        if "MATIC" in self.label:
            if self.token_b == USDC or self.token_b == USDT:
                return self.price_a * matic_price if "MATIC" in self.label.split("/")[0] else self.price_b * matic_price
        if "WETH" in self.label:
            # 用 MATIC 做基准
            pass
        return 0

    def __repr__(self) -> str:
        if not self.ok:
            return f"{self.dex} {self.label}: ❌"
        return f"{self.dex} {self.label}: {self.price_a:.6f}  liq=${self.liquidity_usd:,.0f}"


def discover_all_pairs() -> list:
    """发现所有 DEX × 交易对的 pair 地址"""
    pairs = []
    for dex, factory in FACTORIES.items():
        for ta, tb, label in SCAN_PAIRS:
            addr = get_pair_address(factory, ta, tb)
            if addr:
                pairs.append((dex, ta, tb, label, addr))
    return pairs


def scan_all_pools() -> list[PoolSnapshot]:
    """扫描所有已知 pool 的实时价格"""
    discovered = discover_all_pairs()
    pools = []
    for dex, ta, tb, label, addr in discovered:
        p = PoolSnapshot(dex, ta, tb, label, addr)
        if p.fetch():
            pools.append(p)
    return pools


def find_arbitrage(pools: list[PoolSnapshot]) -> list[dict]:
    """在同一交易对的跨 DEX 价差中寻找套利机会"""
    # 按交易对分组
    by_pair: dict[str, list[PoolSnapshot]] = {}
    for p in pools:
        by_pair.setdefault(p.label, []).append(p)

    opportunities = []
    for label, dexes in by_pair.items():
        if len(dexes) < 2:
            continue
        # 找最低买价和最高卖价
        best_buy = min(dexes, key=lambda d: d.price_a)
        best_sell = max(dexes, key=lambda d: d.price_a)

        if best_buy.price_a <= 0 or best_sell.price_a <= 0:
            continue

        spread_pct = (best_sell.price_a - best_buy.price_a) / best_buy.price_a * 100
        if spread_pct < 0.05:  # 忽略 < 0.05% 的噪音
            continue

        # 估算利润 (假设用 1 个 token_a 交易)
        buy_cost = 1.0  # token_a
        sell_return = best_sell.price_a / best_buy.price_a  # token_a after round trip

        opportunities.append({
            "pair": label,
            "spread_pct": round(spread_pct, 3),
            "buy_on": best_buy.dex,
            "sell_on": best_sell.dex,
            "buy_price": round(best_buy.price_a, 6),
            "sell_price": round(best_sell.price_a, 6),
            "return_per_token": round(sell_return - 1, 6),
        })

    return sorted(opportunities, key=lambda x: x["spread_pct"], reverse=True)


# ══════════════════════════════════════════════════════════════
#  执行引擎
# ══════════════════════════════════════════════════════════════

class ArbitrageBot:
    def __init__(self):
        self.wallet_key = os.getenv("WALLET_KEY", "")
        self.wallet_addr = os.getenv("WALLET_ADDRESS", "0xa66c92bcb095533ed878fc30a4cbd24dc8edde93")
        self.w3 = get_w3()
        self.dry_run = not bool(self.wallet_key and self.w3)
        self.tx_count = 0
        self.total_profit = 0.0

    @property
    def ready(self) -> bool:
        return bool(self.wallet_key and self.w3 and self.wallet_addr != "0x0000000000000000000000000000000000000001")

    def get_balance(self, token: str) -> float:
        w3 = self.w3
        if not w3: return 0.0
        try:
            if token == "":
                bal = w3.eth.get_balance(w3.to_checksum_address(self.wallet_addr))
                return bal / 1e18
            c = w3.eth.contract(address=w3.to_checksum_address(token), abi=ERC20_ABI)
            dec = c.functions.decimals().call()
            return c.functions.balanceOf(w3.to_checksum_address(self.wallet_addr)).call() / (10 ** dec)
        except:
            return 0.0

    def estimate_trade(self, token_in: str, token_out: str, amount: float,
                       buy_dex: str, sell_dex: str) -> dict:
        """估算一笔套利的预期收益"""
        w3 = self.w3
        if not w3:
            return {"error": "Web3 不可用"}

        # 判断 token_in 精度
        c_in = w3.eth.contract(address=w3.to_checksum_address(token_in), abi=ERC20_ABI)
        dec_in = c_in.functions.decimals().call()
        amount_wei = int(amount * (10 ** dec_in))

        buy_router = QUICKSWAP_ROUTER if "Quick" in buy_dex else SUSHISWAP_ROUTER
        sell_router = QUICKSWAP_ROUTER if "Quick" in sell_dex else SUSHISWAP_ROUTER

        try:
            # Step 1: buy_dex token_in → token_out
            r1 = w3.eth.contract(address=w3.to_checksum_address(buy_router), abi=ROUTER_ABI)
            out1 = r1.functions.getAmountsOut(amount_wei, [
                w3.to_checksum_address(token_in), w3.to_checksum_address(token_out)
            ]).call()
            received = out1[1]
            dec_out = w3.eth.contract(address=w3.to_checksum_address(token_out), abi=ERC20_ABI).functions.decimals().call()
            received_float = received / (10 ** dec_out)

            # Step 2: sell_dex token_out → token_in
            r2 = w3.eth.contract(address=w3.to_checksum_address(sell_router), abi=ROUTER_ABI)
            out2 = r2.functions.getAmountsOut(received, [
                w3.to_checksum_address(token_out), w3.to_checksum_address(token_in)
            ]).call()
            back = out2[1]
            back_float = back / (10 ** dec_in)

            gross = back_float - amount
            pct = gross / amount * 100 if amount > 0 else 0

            # Gas
            gas_price = net_gas_price()
            gas_cost_matic = 500000 * gas_price / 1e18
            matic_price = self._matic_price()
            gas_cost_usd = gas_cost_matic * matic_price

            net = gross * matic_price - gas_cost_usd

            return {
                "amount_in": amount,
                "token_in": token_in,
                "token_out": token_out,
                "received": round(received_float, 6),
                "returned": round(back_float, 6),
                "gross": round(gross, 6),
                "gross_pct": round(pct, 3),
                "gas_cost_matic": round(gas_cost_matic, 6),
                "gas_cost_usd": round(gas_cost_usd, 6),
                "net_profit_usd": round(net, 6),
                "profitable": net > MIN_PROFIT_USD,
            }
        except Exception as e:
            return {"error": str(e)}

    def _matic_price(self) -> float:
        """获取 MATIC 当前 USD 价格"""
        try:
            pair = get_pair_address(FACTORIES["QuickSwap"], WMATIC, USDC)
            if pair:
                p = PoolSnapshot("QS", WMATIC, USDC, "MATIC/USDC", pair)
                if p.fetch():
                    return p.price_a
        except:
            pass
        return 0.095

    def execute(self, token_in: str, token_out: str, amount: float,
                buy_dex: str, sell_dex: str) -> dict:
        """执行跨 DEX 套利"""
        result = {
            "timestamp": datetime.now().isoformat(),
            "strategy": f"{buy_dex}→{sell_dex}",
            "amount": amount,
            "status": "dry_run",
        }

        if self.dry_run:
            est = self.estimate_trade(token_in, token_out, amount, buy_dex, sell_dex)
            result["estimate"] = est
            result["note"] = "DRY RUN — 需 WALLET_KEY 才能执行"
            return result

        # LIVE
        if not self.w3:
            result["status"] = "error"
            result["note"] = "Web3 不可用"
            return result

        try:
            w3 = self.w3
            acct = w3.eth.account.from_key(self.wallet_key)
            sender = acct.address
            chain = net_chain_id()
            nonce = w3.eth.get_transaction_count(sender)
            gas_price = w3.eth.gas_price

            c_in = w3.eth.contract(address=w3.to_checksum_address(token_in), abi=ERC20_ABI)
            dec_in = c_in.functions.decimals().call()
            amount_wei = int(amount * (10 ** dec_in))

            buy_router = QUICKSWAP_ROUTER if "Quick" in buy_dex else SUSHISWAP_ROUTER
            sell_router = QUICKSWAP_ROUTER if "Quick" in sell_dex else SUSHISWAP_ROUTER

            # Approve token_in → buy_router
            approve = c_in.functions.approve(
                w3.to_checksum_address(buy_router), amount_wei
            ).build_transaction({
                "from": sender, "nonce": nonce,
                "gas": 80000, "gasPrice": gas_price, "chainId": chain,
            })
            signed = acct.sign_transaction(approve)
            w3.eth.send_raw_transaction(signed.raw_transaction)
            nonce += 1
            time.sleep(0.5)

            # Buy: token_in → token_out on buy_dex
            r1 = w3.eth.contract(address=w3.to_checksum_address(buy_router), abi=ROUTER_ABI)
            out1 = r1.functions.getAmountsOut(amount_wei, [
                w3.to_checksum_address(token_in), w3.to_checksum_address(token_out)
            ]).call()
            min_out = int(out1[1] * (1 - SLIPPAGE))

            tx1 = r1.functions.swapExactTokensForTokens(
                amount_wei, min_out,
                [w3.to_checksum_address(token_in), w3.to_checksum_address(token_out)],
                sender, int(time.time()) + 300
            ).build_transaction({
                "from": sender, "nonce": nonce,
                "gas": 300000, "gasPrice": gas_price, "chainId": chain,
            })
            signed1 = acct.sign_transaction(tx1)
            tx1_hash = w3.eth.send_raw_transaction(signed1.raw_transaction)
            receipt1 = w3.eth.wait_for_transaction_receipt(tx1_hash)
            result["tx1"] = {"hash": tx1_hash.hex(), "gas": receipt1["gasUsed"]}
            nonce += 1

            # 获取实际收到的 token_out
            c_out = w3.eth.contract(address=w3.to_checksum_address(token_out), abi=ERC20_ABI)
            dec_out = c_out.functions.decimals().call()
            received = c_out.functions.balanceOf(sender).call()

            # Approve token_out → sell_router
            approve2 = c_out.functions.approve(
                w3.to_checksum_address(sell_router), received
            ).build_transaction({
                "from": sender, "nonce": nonce,
                "gas": 80000, "gasPrice": gas_price, "chainId": chain,
            })
            signed2 = acct.sign_transaction(approve2)
            w3.eth.send_raw_transaction(signed2.raw_transaction)
            nonce += 1
            time.sleep(0.5)

            # Sell: token_out → token_in on sell_dex
            r2 = w3.eth.contract(address=w3.to_checksum_address(sell_router), abi=ROUTER_ABI)
            out2 = r2.functions.getAmountsOut(received, [
                w3.to_checksum_address(token_out), w3.to_checksum_address(token_in)
            ]).call()
            min_back = int(out2[1] * (1 - SLIPPAGE))

            tx2 = r2.functions.swapExactTokensForTokens(
                received, min_back,
                [w3.to_checksum_address(token_out), w3.to_checksum_address(token_in)],
                sender, int(time.time()) + 300
            ).build_transaction({
                "from": sender, "nonce": nonce,
                "gas": 300000, "gasPrice": gas_price, "chainId": chain,
            })
            signed3 = acct.sign_transaction(tx2)
            tx2_hash = w3.eth.send_raw_transaction(signed3.raw_transaction)
            receipt2 = w3.eth.wait_for_transaction_receipt(tx2_hash)
            result["tx2"] = {"hash": tx2_hash.hex(), "gas": receipt2["gasUsed"]}

            # Profit
            final_bal = c_in.functions.balanceOf(sender).call()
            profit_wei = final_bal - amount_wei if final_bal > amount_wei else 0
            result["profit"] = profit_wei / (10 ** dec_in)
            result["status"] = "executed"
            self.tx_count += 1

        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)

        return result


# ══════════════════════════════════════════════════════════════
#  CLI 命令
# ══════════════════════════════════════════════════════════════

def cmd_scan():
    print(f"\n{'='*50}")
    print(f"Polygon DEX 价差扫描 v2")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")

    gas = net_gas_price() / 1e9
    block = net_block()
    print(f"区块: {block:,}  |  Gas: {gas:.1f} gwei\n")

    pools = scan_all_pools()
    if not pools:
        print("❌ 无法获取任何 pool 数据")
        return

    # 显示所有 pool 价格
    print("交易对价格:")
    for p in pools:
        tok_a = "MATIC" if "MATIC" in p.label else "WETH"
        print(f"  {p.dex:10s} {p.label:12s}  1 {tok_a:5s} = {p.price_a:.6f}  (流动性: ${p.liquidity_usd:,.0f})")
    print()

    # 价差分析
    arb = find_arbitrage(pools)
    if arb:
        print("价差机会:")
        for a in arb:
            flag = "✅" if a["spread_pct"] > 0.5 else "⏳"
            print(f"  {flag} {a['pair']:12s}  价差={a['spread_pct']:.3f}%  "
                  f"买: {a['buy_on']:10s}  卖: {a['sell_on']:10s}")
    else:
        print("⏳ 无可用的价差机会")

    # 利润估算
    bot = ArbitrageBot()
    wallet_usdt = bot.get_balance(USDT)
    wallet_usdc = bot.get_balance(USDC)
    wallet_matic = bot.get_balance("")
    print(f"\n钱包:")
    print(f"  MATIC: {wallet_matic:.4f}")
    print(f"  USDC:  {wallet_usdc:.4f}")
    print(f"  USDT:  {wallet_usdt:.4f}")
    print(f"  模式:   {'LIVE' if bot.ready else 'DRY RUN'}")

    if not bot.ready:
        print(f"\n⚠️  需要私钥才能交易")
        print(f"  MetaMask → 钱包详情 → 导出私钥 → 写入 .env  WALLET_KEY=")

    print(f"\n{'='*50}\n")


def cmd_trade():
    bot = ArbitrageBot()
    pools = scan_all_pools()
    arb = find_arbitrage(pools)

    if not arb:
        print("❌ 无可执行的套利机会")
        return

    best = arb[0]
    print(f"最佳机会: {best['pair']}  价差={best['spread_pct']:.2f}%")
    print(f"  {best['buy_on']} → {best['sell_on']}")

    # 用 USDT 本金
    usdt_bal = bot.get_balance(USDT)
    amount = min(usdt_bal, 5.0)

    if amount < 0.5:
        print(f"❌ USDT 余额不足 (${usdt_bal:.2f})")
        return

    # 判断 token_in 和 token_out
    if best["pair"] == "MATIC/USDC" or best["pair"] == "MATIC/USDT":
        token_in = USDT if "USDT" in best["pair"] else USDC
        token_out = WMATIC
    elif best["pair"] == "WETH/USDC":
        token_in = USDC
        token_out = WETH
    else:
        token_in = USDT
        token_out = WETH

    result = bot.execute(token_in, token_out, amount, best["buy_on"], best["sell_on"])
    print(f"\n结果: {result['status']}")
    if "estimate" in result:
        e = result["estimate"]
        if "error" not in e:
            print(f"  毛利: {e['gross']:.6f} ({e['gross_pct']:.2f}%)")
            print(f"  Gas:  ${e['gas_cost_usd']:.4f}")
            print(f"  净利: ${e['net_profit_usd']:.4f}")
        else:
            print(f"  估算错误: {e['error']}")
    if "note" in result:
        print(f"  {result['note']}")


def cmd_monitor():
    bot = ArbitrageBot()
    print(f"\n{'='*50}")
    print(f"Polygon 持续监控 {'LIVE' if bot.ready else 'DRY RUN'}")
    print(f"{'='*50}\n")

    for rnd in range(30):
        now = datetime.now().strftime("%H:%M:%S")
        gas = net_gas_price() / 1e9
        block = net_block()
        pools = scan_all_pools()
        arb = find_arbitrage(pools)

        line = f"[{now}] block={block:,} gas={gas:.1f}"
        if arb:
            best = arb[0]
            line += f" 最佳={best['pair']} {best['spread_pct']:.2f}%"

            if best["spread_pct"] > 0.5 and bot.ready:
                usdt = bot.get_balance(USDT)
                if usdt >= 0.5:
                    if "USDT" in best["pair"]:
                        ti, to = USDT, WMATIC
                    else:
                        ti, to = USDC, WMATIC
                    r = bot.execute(ti, to, min(usdt, 3), best["buy_on"], best["sell_on"])
                    profit = r.get("estimate", {}).get("net_profit_usd", 0) if "estimate" in r else r.get("profit", 0)
                    line += f" ✅ ${profit:.4f}"
                else:
                    line += " ⏳ 余额不足"
            elif best["spread_pct"] > 0.5:
                line += " 🟢 可执行(需私钥)"
            else:
                line += " ⏳ 等待"
        else:
            line += " 无机会"

        print(line)
        time.sleep(10)

    print("\n监控结束 (30 轮)")


def cmd_status():
    bot = ArbitrageBot()
    w3 = get_w3()
    print(f"\n{'='*50}")
    print(f"Polygon 状态")
    print(f"{'='*50}")
    print(f"RPC: {POLYGON_RPC}")
    print(f"区块: {net_block():,}")
    print(f"Gas: {net_gas_price()/1e9:.1f} gwei")
    print(f"链ID: {net_chain_id()}")
    print(f"Web3: {'✅' if w3 else '❌'}")
    print(f"\n钱包: {bot.wallet_addr}")
    print(f"  MATIC: {bot.get_balance(''):.4f}")
    print(f"  USDC:  {bot.get_balance(USDC):.4f}")
    print(f"  USDT:  {bot.get_balance(USDT):.4f}")
    print(f"  模式:   {'LIVE' if bot.ready else 'DRY RUN'}")
    print(f"\n{'='*50}\n")


def cmd_balances():
    print(f"\n{'='*50}")
    print(f"帝国钱包总览")
    print(f"{'='*50}")

    bot = ArbitrageBot()
    matic = bot.get_balance("")
    usdc = bot.get_balance(USDC)
    usdt = bot.get_balance(USDT)
    print(f"\nPolygon ({bot.wallet_addr}):")
    print(f"  MATIC: {matic:.4f}  (≈ ${matic * 0.096:.2f})")
    print(f"  USDC:  {usdc:.4f}")
    print(f"  USDT:  {usdt:.4f}")
    print(f"  总计:  ≈ ${matic * 0.096 + usdc + usdt:.2f}")

    try:
        from solana_monitor import make_rpc_call as sol_rpc
        sol_r = sol_rpc("getBalance", ["BvXqSW5Fwc6LMTyJopbRkQPLYDQFV9hEfR5sMthq73m8"])
        sol_bal = sol_r.get("result", {}).get("value", 0) / 1e9
        print(f"\nSolana (BvXqSW5Fwc6LMTyJopbRkQPLYDQFV9hEfR5sMthq73m8):")
        print(f"  SOL:  {sol_bal:.4f}  (≈ ${sol_bal * 160:.2f})")
    except Exception:
        print(f"\nSolana: 查询失败")

    try:
        from evm_arb import RPCClient as EvmRPC
        evm = EvmRPC()
        eth_bal = evm.get_balance("0xa66c92bcb095533ed878fc30a4cbd24dc8edde93") / 1e18
        print(f"\nEthereum (0xa66c92bcb095533ed878fc30a4cbd24dc8edde93):")
        print(f"  ETH:  {eth_bal:.6f}")
    except Exception:
        print(f"\nEthereum: 查询失败")

    total = matic * 0.096 + usdc + usdt + sol_bal * 160 + eth_bal * 2800 if 'sol_bal' in dir() and 'eth_bal' in dir() else 0
    print(f"\n总资产: ≈ ${total:.2f}")
    print(f"{'='*50}\n")


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "scan"
    cmds = {
        "scan": cmd_scan, "trade": cmd_trade,
        "monitor": cmd_monitor, "status": cmd_status,
        "balances": cmd_balances,
    }
    if action in cmds:
        cmds[action]()
    else:
        print(f"用法: python polygon_swapper.py [scan|trade|monitor|status|balances]")


if __name__ == "__main__":
    main()
