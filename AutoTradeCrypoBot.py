# --- Bibliotecas Essenciais ---
import telegram
from telegram.ext import Application, CommandHandler
import logging
import time
import os
from dotenv import load_dotenv
import asyncio
from base64 import b64decode
import pandas as pd
import pandas_ta as ta
import httpx
from datetime import datetime, timedelta

# --- Libs da Solana ---
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from spl.token.instructions import get_associated_token_address

# --- Servidor Web para Keep-Alive ---
from flask import Flask
from threading import Thread

app = Flask('')
@app.route('/')
def home():
    return "Bot is alive!"
def run_server():
    app.run(host='0.0.0.0', port=8080)
def keep_alive():
    t = Thread(target=run_server)
    t.start()

# --- Carregamento de Variáveis de Ambiente ---
load_dotenv()

# --- Configurações Iniciais e Validação ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
PRIVATE_KEY_B58 = os.getenv("PRIVATE_KEY_BASE58")
RPC_URL = os.getenv("RPC_URL")

if not all([TELEGRAM_TOKEN, CHAT_ID, PRIVATE_KEY_B58, RPC_URL]):
    print("ERRO: Verifique as variáveis de ambiente: TELEGRAM_TOKEN, CHAT_ID, PRIVATE_KEY_BASE58, RPC_URL")
    exit()

# --- Configuração do Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)

# --- Cliente Solana e Carteira ---
try:
    payer = Keypair.from_base58_string(PRIVATE_KEY_B58)
    solana_client = Client(RPC_URL)
    logger.info(f"Carteira carregada: {payer.pubkey()}")
except Exception as e:
    logger.error(f"Erro ao carregar a carteira Solana: {e}"); exit()

# --- Constantes e Variáveis Globais ---
bot_running = False
in_position = False
entry_price = 0.0
position_high_price = 0.0
check_interval_seconds = 3600
periodic_task = None
parameters = {
    "base_token_symbol": None, "quote_token_symbol": None, "timeframe": None,
    "ma_period": None, "amount": None,
    "take_profit_percent": None, "trailing_stop_percent": None,
    "trade_pair_details": {}
}
application = None

# --- Funções de Execução de Ordem (Jupiter API) ---
async def execute_swap(input_mint_str, output_mint_str, amount, input_decimals, slippage_bps=1000):
    logger.info(f"Iniciando swap de {amount} de {input_mint_str} para {output_mint_str}")
    amount_wei = int(amount * (10**input_decimals))
    async with httpx.AsyncClient() as client:
        try:
            quote_url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint_str}&outputMint={output_mint_str}&amount={amount_wei}&slippageBps={slippage_bps}"
            quote_res = await client.get(quote_url, timeout=30.0); quote_res.raise_for_status(); quote_response = quote_res.json()
            swap_payload = {"userPublicKey": str(payer.pubkey()), "quoteResponse": quote_response, "wrapAndUnwrapSol": True}
            swap_url = "https://quote-api.jup.ag/v6/swap"
            swap_res = await client.post(swap_url, json=swap_payload, timeout=30.0); swap_res.raise_for_status(); swap_response = swap_res.json()
            swap_tx_b64 = swap_response.get('swapTransaction')
            if not swap_tx_b64: logger.error(f"Erro na API Jupiter: {swap_response}"); return None
            raw_tx_bytes = b64decode(swap_tx_b64); swap_tx = VersionedTransaction.from_bytes(raw_tx_bytes)
            signature = payer.sign_message(to_bytes_versioned(swap_tx.message)); signed_tx = VersionedTransaction.populate(swap_tx.message, [signature])
            tx_opts = TxOpts(skip_preflight=False, preflight_commitment="confirmed")
            tx_signature = solana_client.send_raw_transaction(bytes(signed_tx), opts=tx_opts).value
            logger.info(f"Transação enviada: {tx_signature}"); solana_client.confirm_transaction(tx_signature, commitment="confirmed")
            logger.info(f"Transação confirmada: https://solscan.io/tx/{tx_signature}"); return str(tx_signature)
        except Exception as e: logger.error(f"Falha na transação: {e}"); await send_telegram_message(f"⚠️ Falha on-chain: {e}"); return None
async def execute_buy_order(amount, price_usd):
    global in_position, entry_price, position_high_price
    details = parameters["trade_pair_details"]; logger.info(f"EXECUTANDO COMPRA de {amount} {details['quote_symbol']} para {details['base_symbol']} a ${price_usd}")
    tx_sig = await execute_swap(details['quote_address'], details['base_address'], amount, details['quote_decimals'])
    if tx_sig:
        in_position = True; entry_price = price_usd; position_high_price = price_usd
        await send_telegram_message(f"✅ COMPRA: {amount} {details['quote_symbol']} para {details['base_symbol']}\nPreço Entrada: ${price_usd:.6f}\nhttps://solscan.io/tx/{tx_sig}")
    else:
        in_position = False; entry_price = 0.0; position_high_price = 0.0
        await send_telegram_message(f"❌ FALHA NA COMPRA de {details['base_symbol']}")
async def execute_sell_order(reason="Venda Manual"):
    global in_position, entry_price, position_high_price
    details = parameters["trade_pair_details"]; logger.info(f"EXECUTANDO VENDA de {details['base_symbol']}. Motivo: {reason}")
    try:
        token_mint_pubkey = Pubkey.from_string(details['base_address']); ata_address = get_associated_token_address(payer.pubkey(), token_mint_pubkey)
        balance_response = solana_client.get_token_account_balance(ata_address); token_balance_data = balance_response.value
        amount_to_sell_wei = int(token_balance_data.amount); token_decimals = token_balance_data.decimals
        amount_to_sell = amount_to_sell_wei / (10**token_decimals)
        if amount_to_sell_wei == 0: logger.warning("Saldo zero. Resetando posição."); in_position = False; entry_price = 0.0; position_high_price = 0.0; return
        tx_sig = await execute_swap(details['base_address'], details['quote_address'], amount_to_sell, token_decimals)
        if tx_sig:
            in_position = False; entry_price = 0.0; position_high_price = 0.0
            await send_telegram_message(f"🛑 VENDA: {amount_to_sell:.6f} {details['base_symbol']}\nMotivo: {reason}\nhttps://solscan.io/tx/{tx_sig}")
        else:
            await send_telegram_message(f"❌ FALHA NA VENDA de {details['base_symbol']}")
    except Exception as e: logger.error(f"Erro na venda: {e}"); await send_telegram_message(f"⚠️ Falha ao buscar saldo: {e}")

# --- Funções de Obtenção de Dados ---
async def fetch_geckoterminal_ohlcv(pair_address, timeframe):
    timeframe_map = {"1m": "minute", "5m": "minute", "15m": "minute", "1h": "hour", "4h": "hour", "1d": "day"}
    aggregate_map = {"1m": 1, "5m": 5, "15m": 15, "1h": 1, "4h": 4, "1d": 1}
    gt_timeframe, gt_aggregate = timeframe_map.get(timeframe), aggregate_map.get(timeframe)
    if not gt_timeframe: return None
    url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair_address}/ohlcv/{gt_timeframe}?aggregate={gt_aggregate}&limit=300"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=15.0); response.raise_for_status(); api_data = response.json()
            if api_data.get('data') and api_data['data'].get('attributes', {}).get('ohlcv_list'):
                ohlcv_list = api_data['data']['attributes']['ohlcv_list']
                df = pd.DataFrame(ohlcv_list, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
                for col in ['Open', 'High', 'Low', 'Close', 'Volume']: df[col] = pd.to_numeric(df[col])
                return df.sort_values(by='timestamp').reset_index(drop=True)
            logger.warning(f"GeckoTerminal não retornou dados de velas: {api_data}"); return None
    except Exception as e: logger.error(f"Erro ao buscar dados no GeckoTerminal: {e}"); return None
async def fetch_dexscreener_prices(pair_address):
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0); res.raise_for_status(); pair_data = res.json().get('pair')
            if not pair_data: return None
            price_usd_str = pair_data.get('priceUsd')
            return {"price_usd": float(price_usd_str) if price_usd_str else None}
    except Exception as e: logger.error(f"Erro ao buscar preços no Dexscreener: {e}"); return None

# --- LÓGICA CENTRAL DA ESTRATÉGIA (BASEADA EM USD) ---
async def check_strategy():
    global in_position, entry_price, position_high_price
    if not bot_running or not all(p is not None for p in {k: v for k, v in parameters.items() if k != 'trade_pair_details'}): return
    try:
        pair_details = parameters["trade_pair_details"]; timeframe, ma_period = parameters["timeframe"], int(parameters["ma_period"])
        amount = parameters["amount"]; take_profit_percent = parameters["take_profit_percent"]; trailing_stop_percent = parameters["trailing_stop_percent"]
        
        logger.info("Coletando dados de Dexscreener (real-time) e GeckoTerminal (histórico)...")
        price_data = await fetch_dexscreener_prices(pair_details['pair_address'])
        historic_data = await fetch_geckoterminal_ohlcv(pair_details['pair_address'], timeframe)
        
        if not price_data or historic_data is None or historic_data.empty: await send_telegram_message("⚠️ Falha ao obter dados. Análise abortada."); return
        real_time_price_usd = price_data.get('price_usd')
        if not real_time_price_usd or len(historic_data) < ma_period: await send_telegram_message("⚠️ Dados insuficientes. Análise abortada."); return
        
        # Lógica de cálculo toda em USD
        sma_col = f'SMA_{ma_period}'; historic_data.ta.sma(close=historic_data['Close'], length=ma_period, append=True)
        previous_candle = historic_data.iloc[-2]; current_candle = historic_data.iloc[-1]
        current_sma_usd = current_candle[sma_col]; previous_close_usd = previous_candle['Close']
        
        logger.info(f"Análise (USD): Preço ${real_time_price_usd:.8f} | Média ${current_sma_usd:.8f}")

        if in_position:
            if real_time_price_usd > position_high_price: position_high_price = real_time_price_usd
            take_profit_target_usd = entry_price * (1 + take_profit_percent / 100)
            trailing_stop_price_usd = position_high_price * (1 - trailing_stop_percent / 100)
            logger.info(f"Posição Aberta: Entrada ${entry_price:.6f}, Máxima ${position_high_price:.6f}, Alvo TP ${take_profit_target_usd:.6f}, Stop Móvel ${trailing_stop_price_usd:.6f}")
            if real_time_price_usd >= take_profit_target_usd: await execute_sell_order(reason=f"Take Profit atingido em ${take_profit_target_usd:.6f}"); return
            if real_time_price_usd <= trailing_stop_price_usd: await execute_sell_order(reason=f"Trailing Stop atingido em ${trailing_stop_price_usd:.6f}"); return
            sell_signal = previous_close_usd > current_sma_usd and real_time_price_usd < current_sma_usd
            if sell_signal: await execute_sell_order(reason="Cruzamento de Média Móvel"); return
        
        buy_signal = previous_close_usd < current_sma_usd and real_time_price_usd > current_sma_usd
        if not in_position and buy_signal: logger.info("Sinal de COMPRA detectado."); await execute_buy_order(amount, real_time_price_usd)
    except Exception as e: logger.error(f"Erro em check_strategy: {e}", exc_info=True); await send_telegram_message(f"⚠️ Erro inesperado na estratégia: {e}")

# --- Funções e Comandos do Telegram ---
async def send_telegram_message(message):
    if application: await application.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')
async def start(update, context):
    await update.effective_message.reply_text(
        'Olá! Sou seu bot de autotrade para a rede Solana.\n\n'
        '**Fonte de Dados:** `GeckoTerminal + Dexscreener`\n'
        '**Negociação:** `Jupiter`\n\n'
        'Use `/set` para configurar com o **ENDEREÇO DO PAR**:\n'
        '`/set <ENDEREÇO_DO_PAR> <TF> <MA> <VALOR> <TP_%> <TS_%>`\n\n'
        '**Exemplo (WIF/SOL):**\n'
        '`/set EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzL7M6fV2zY2g6 1h 21 0.1 25 10`\n\n'
        '**Importante:** Use o endereço do PAR de maior liquidez que você encontra no Dexscreener.\n\n'
        '**Comandos:**\n`/run` | `/stop` | `/buy` | `/sell`', parse_mode='Markdown')
async def set_params(update, context):
    global parameters, check_interval_seconds
    if bot_running: await update.effective_message.reply_text("Pare o bot com /stop antes."); return
    try:
        if len(context.args) != 6:
            await update.effective_message.reply_text("⚠️ *Erro: Formato incorreto.*\nUse: `/set <ENDEREÇO_DO_PAR> <TF> <MA> <VALOR> <TP_%> <TS_%>`", parse_mode='Markdown'); return
        
        pair_address = context.args[0]; timeframe, ma_period = context.args[1].lower(), int(context.args[2])
        amount = float(context.args[3]); take_profit_percent = float(context.args[4]); trailing_stop_percent = float(context.args[5])
        
        interval_map = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
        if timeframe not in interval_map: await update.effective_message.reply_text(f"⚠️ Timeframe '{timeframe}' não suportado."); return
        check_interval_seconds = interval_map[timeframe]
        
        pair_search_url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
        async with httpx.AsyncClient() as client:
            response = await client.get(pair_search_url); response.raise_for_status(); pair_res = response.json()
        
        trade_pair = pair_res.get('pair')
        if not trade_pair: await update.effective_message.reply_text(f"⚠️ Nenhum par encontrado para o endereço fornecido."); return

        base_token_symbol = trade_pair['baseToken']['symbol'].lstrip('$'); quote_token_symbol = trade_pair['quoteToken']['symbol']
        
        parameters = {
            "base_token_symbol": base_token_symbol, "quote_token_symbol": quote_token_symbol, "timeframe": timeframe,
            "ma_period": ma_period, "amount": amount,
            "take_profit_percent": take_profit_percent, "trailing_stop_percent": trailing_stop_percent,
            "trade_pair_details": { 
                "base_symbol": base_token_symbol, "quote_symbol": quote_token_symbol, 
                "base_address": trade_pair['baseToken']['address'], "quote_address": trade_pair['quoteToken']['address'], 
                "pair_address": trade_pair['pairAddress'], 
                "quote_decimals": 9 if quote_token_symbol in ['SOL', 'WSOL'] else 6 
            }
        }
        await update.effective_message.reply_text(
            f"✅ *Parâmetros definidos!*\n\n"
            f"🪙 *Par:* `{base_token_symbol}/{quote_token_symbol}`\n"
            f"*Endereço do Par:* `{trade_pair['pairAddress']}`\n"
            f"⏰ *Timeframe:* `{timeframe}` | *MA:* `{ma_period}`\n"
            f"💰 *Valor/Ordem:* `{amount}` {quote_token_symbol}\n"
            f"📈 *Take Profit:* `{take_profit_percent}%`\n"
            f"📉 *Trailing Stop:* `{trailing_stop_percent}%`", parse_mode='Markdown')
    except Exception as e: logger.error(f"Erro em set_params: {e}"); await update.effective_message.reply_text(f"⚠️ Erro ao configurar: {e}")

# --- Outras funções do bot (run, stop, buy, sell, etc) ---
async def run_bot(update, context):
    global bot_running, periodic_task
    if not all(p is not None for p in {k: v for k, v in parameters.items() if k != 'trade_pair_details'}): await update.effective_message.reply_text("Defina os parâmetros com /set primeiro."); return
    if bot_running: await update.effective_message.reply_text("O bot já está em execução."); return
    bot_running = True; logger.info("Bot de trade iniciado."); await update.effective_message.reply_text("🚀 Bot iniciado!")
    if periodic_task is None or periodic_task.done(): periodic_task = asyncio.create_task(periodic_checker())
    await check_strategy()
async def stop_bot(update, context):
    global bot_running, in_position, entry_price, position_high_price, periodic_task
    if not bot_running: await update.effective_message.reply_text("O bot já está parado."); return
    bot_running = False
    if periodic_task: periodic_task.cancel(); periodic_task = None
    in_position, entry_price, position_high_price = False, 0.0, 0.0
    logger.info("Bot de trade parado."); await update.effective_message.reply_text("🛑 Bot parado.")
async def manual_buy(update, context):
    if not bot_running: await update.effective_message.reply_text("Use /run primeiro."); return
    if in_position: await update.effective_message.reply_text("Já existe uma posição aberta."); return
    logger.info("Forçando compra..."); await update.effective_message.reply_text("Forçando ordem de compra...")
    try:
        price_data = await fetch_dexscreener_prices(parameters['trade_pair_details']['pair_address'])
        real_time_price_usd = price_data.get('price_usd') if price_data else None
        if real_time_price_usd and real_time_price_usd > 0: await execute_buy_order(parameters["amount"], real_time_price_usd)
        else: raise ValueError("Preço em USD inválido ou nulo")
    except Exception as e: logger.error(f"Erro na compra manual: {e}"); await update.effective_message.reply_text("⚠️ Falha ao obter o preço para compra.")
async def manual_sell(update, context):
    if not bot_running: await update.effective_message.reply_text("Use /run primeiro."); return
    if not in_position: await update.effective_message.reply_text("Nenhuma posição aberta."); return
    logger.info("Forçando venda..."); await update.effective_message.reply_text("Forçando ordem de venda...")
    await execute_sell_order()
async def periodic_checker():
    logger.info(f"Verificador periódico iniciado: intervalo de {check_interval_seconds}s.")
    while True:
        try:
            await asyncio.sleep(check_interval_seconds)
            if bot_running: logger.info("Executando verificação periódica..."); await check_strategy()
        except asyncio.CancelledError: logger.info("Verificador periódico cancelado."); break
        except Exception as e: logger.error(f"Erro no loop periódico: {e}"); await asyncio.sleep(60)
def main():
    global application
    keep_alive()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start)); application.add_handler(CommandHandler("set", set_params))
    application.add_handler(CommandHandler("run", run_bot)); application.add_handler(CommandHandler("stop", stop_bot))
    application.add_handler(CommandHandler("buy", manual_buy)); application.add_handler(CommandHandler("sell", manual_sell))
    logger.info("Bot do Telegram iniciado..."); application.run_polling()

if __name__ == '__main__':
    main()
