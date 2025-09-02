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
import httpx  # Biblioteca para requisições assíncronas

# --- Libs da Solana ---
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from spl.token.instructions import get_associated_token_address

# --- Servidor Web para Keep-Alive (Replit, etc.) ---
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
    print("ERRO: Verifique se todas as variáveis de ambiente estão definidas:")
    print("TELEGRAM_TOKEN, CHAT_ID, PRIVATE_KEY_BASE58, RPC_URL")
    exit()

# --- Configuração do Logging ---
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# --- Cliente Solana e Carteira ---
try:
    payer = Keypair.from_base58_string(PRIVATE_KEY_B58)
    solana_client = Client(RPC_URL)
    logger.info(f"Carteira carregada com sucesso. Endereço público: {payer.pubkey()}")
except Exception as e:
    logger.error(f"Erro ao carregar a carteira Solana. Verifique sua chave privada e o RPC URL. Erro: {e}")
    exit()

# --- Constantes e Variáveis Globais ---
# Par SOL/USDC de alta liquidez da Raydium para fallback
SOL_USDC_PAIR_ADDRESS = "58oQChx4yWmvKdwLLZzBi4ChoCc2fqb_AB2M_AcV1G_c"
bot_running = False
in_position = False
entry_price = 0.0  # Sempre armazenado em USD
check_interval_seconds = 3600
periodic_task = None
parameters = {
    "base_token_symbol": None,
    "quote_token_symbol": None,
    "timeframe": None,
    "ma_period": None,
    "amount": None,
    "stop_loss_percent": None,
    "trade_pair_details": {}
}
application = None

# --- Funções de Execução de Ordem (Jupiter API) ---
async def execute_swap(input_mint_str, output_mint_str, amount, input_decimals, slippage_bps=100):
    logger.info(f"Iniciando swap de {amount} de {input_mint_str} para {output_mint_str}")
    amount_wei = int(amount * (10**input_decimals))
    async with httpx.AsyncClient() as client:
        try:
            quote_url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint_str}&outputMint={output_mint_str}&amount={amount_wei}&slippageBps={slippage_bps}"
            quote_res = await client.get(quote_url, timeout=20.0)
            quote_res.raise_for_status()
            quote_response = quote_res.json()

            swap_payload = {"userPublicKey": str(payer.pubkey()), "quoteResponse": quote_response, "wrapAndUnwrapSol": True}
            swap_url = "https://quote-api.jup.ag/v6/swap"
            swap_res = await client.post(swap_url, json=swap_payload, timeout=20.0)
            swap_res.raise_for_status()
            swap_response = swap_res.json()
            swap_tx_b64 = swap_response.get('swapTransaction')
            if not swap_tx_b64:
                logger.error(f"Erro na resposta da API de swap da Jupiter: {swap_response}"); return None

            raw_tx_bytes = b64decode(swap_tx_b64)
            swap_tx = VersionedTransaction.from_bytes(raw_tx_bytes)
            signature = payer.sign_message(to_bytes_versioned(swap_tx.message))
            signed_tx = VersionedTransaction.populate(swap_tx.message, [signature])

            tx_opts = TxOpts(skip_preflight=False, preflight_commitment="confirmed")
            tx_signature = solana_client.send_raw_transaction(bytes(signed_tx), opts=tx_opts).value

            logger.info(f"Transação enviada com sucesso! Assinatura: {tx_signature}")
            solana_client.confirm_transaction(tx_signature, commitment="confirmed")
            logger.info(f"Transação confirmada! Link: https://solscan.io/tx/{tx_signature}")
            return str(tx_signature)

        except httpx.HTTPStatusError as e:
            logger.error(f"Erro de HTTP na API da Jupiter: {e.response.text}"); await send_telegram_message(f"⚠️ Falha na comunicação com a Jupiter: {e.response.text}"); return None
        except Exception as e:
            logger.error(f"Falha na transação: {e}"); await send_telegram_message(f"⚠️ Falha na transação on-chain: {e}"); return None

async def execute_buy_order(amount, price_usd):
    global in_position, entry_price
    details = parameters["trade_pair_details"]
    logger.info(f"EXECUTANDO ORDEM DE COMPRA REAL de {amount} {details['quote_symbol']} para {details['base_symbol']} ao preço de {price_usd} USD")
    entry_price = price_usd
    tx_sig = await execute_swap(details['quote_address'], details['base_address'], amount, details['quote_decimals'])
    if tx_sig:
        in_position = True
        await send_telegram_message(f"✅ COMPRA REALIZADA: {amount} {details['quote_symbol']} para {details['base_symbol']}\nPreço de Entrada: ${price_usd:.6f}\nhttps://solscan.io/tx/{tx_sig}")
    else:
        entry_price = 0.0
        await send_telegram_message(f"❌ FALHA NA COMPRA do token {details['base_symbol']}")

async def execute_sell_order(reason="Venda Manual"):
    global in_position, entry_price
    details = parameters["trade_pair_details"]
    logger.info(f"EXECUTANDO ORDEM DE VENDA REAL do token {details['base_symbol']}. Motivo: {reason}")
    try:
        token_mint_pubkey = Pubkey.from_string(details['base_address'])
        ata_address = get_associated_token_address(payer.pubkey(), token_mint_pubkey)
        balance_response = solana_client.get_token_account_balance(ata_address)
        token_balance_data = balance_response.value
        amount_to_sell_wei = int(token_balance_data.amount)
        token_decimals = token_balance_data.decimals
        amount_to_sell = amount_to_sell_wei / (10**token_decimals)

        if amount_to_sell_wei == 0:
            logger.warning("Tentativa de venda com saldo zero. Resetando posição."); in_position = False; entry_price = 0.0; return

        tx_sig = await execute_swap(details['base_address'], details['quote_address'], amount_to_sell, token_decimals)
        if tx_sig:
            in_position = False; entry_price = 0.0
            await send_telegram_message(f"🛑 VENDA REALIZADA: {amount_to_sell:.6f} de {details['base_symbol']}\nMotivo: {reason}\nhttps://solscan.io/tx/{tx_sig}")
        else:
            await send_telegram_message(f"❌ FALHA NA VENDA do token {details['base_symbol']}")
    except Exception as e:
        logger.error(f"Erro ao buscar saldo para venda: {e}"); await send_telegram_message(f"⚠️ Falha ao buscar saldo do token para venda: {e}")

# --- Funções de Obtenção de Dados ---
async def fetch_geckoterminal_ohlcv(pair_address, timeframe):
    """Busca o histórico de velas no GeckoTerminal."""
    timeframe_map = {"1m": "minute", "5m": "minute", "15m": "minute", "1h": "hour", "4h": "hour", "1d": "day"}
    aggregate_map = {"1m": 1, "5m": 5, "15m": 15, "1h": 1, "4h": 4, "1d": 1}
    gt_timeframe, gt_aggregate = timeframe_map.get(timeframe), aggregate_map.get(timeframe)
    if not gt_timeframe: return None
    url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair_address}/ohlcv/{gt_timeframe}?aggregate={gt_aggregate}&limit=300"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=10.0)
            response.raise_for_status()
            api_data = response.json()
            if api_data.get('data') and api_data['data'].get('attributes', {}).get('ohlcv_list'):
                ohlcv_list = api_data['data']['attributes']['ohlcv_list']
                df = pd.DataFrame(ohlcv_list, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
                for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
                    df[col] = pd.to_numeric(df[col])
                return df.sort_values(by='timestamp').reset_index(drop=True)
            return None
    except Exception as e:
        logger.error(f"Erro ao buscar dados no GeckoTerminal: {e}"); return None

async def fetch_dexscreener_prices(pair_address):
    """Busca preços (USD e Nativo) no Dexscreener."""
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0)
            res.raise_for_status()
            pair_data = res.json().get('pair')
            if not pair_data: return None
            price_usd_str = pair_data.get('priceUsd')
            price_native_str = pair_data.get('priceNative')
            return {
                "pair_price_usd": float(price_usd_str) if price_usd_str else None,
                "pair_price_native": float(price_native_str) if price_native_str else None,
            }
    except Exception as e:
        logger.error(f"Erro ao buscar preços no Dexscreener: {e}"); return None

# --- LÓGICA CENTRAL DA ESTRATÉGIA ---
async def check_strategy():
    global in_position, entry_price
    if not bot_running or not all(p is not None for p in parameters.values() if p != parameters['trade_pair_details']): return

    try:
        pair_details = parameters["trade_pair_details"]
        timeframe, ma_period = parameters["timeframe"], int(parameters["ma_period"])
        amount, stop_loss_percent = parameters["amount"], parameters["stop_loss_percent"]

        # 1. Busca os preços em tempo real
        logger.info("Buscando preços em tempo real no Dexscreener...")
        price_data = await fetch_dexscreener_prices(pair_details['pair_address'])
        if not price_data or price_data.get('pair_price_usd') is None or price_data.get('pair_price_native') is None:
            await send_telegram_message("⚠️ Não foi possível obter os preços completos (USD e Nativo) do Dexscreener.")
            return

        real_time_price_usd = price_data['pair_price_usd']
        real_time_price_native = price_data['pair_price_native']

        # 2. Busca dados históricos (preços na moeda nativa, ex: SOL)
        data = await fetch_geckoterminal_ohlcv(pair_details['pair_address'], timeframe)
        if data is None or data.empty or len(data) < ma_period + 2:
            await send_telegram_message("⚠️ Dados históricos insuficientes para a análise.")
            return

        # 3. Calcula a Média Móvel na moeda nativa (ex: SOL)
        sma_col = f'SMA_{ma_period}'
        data.ta.sma(close=data['Close'], length=ma_period, append=True)
        
        previous_candle = data.iloc[-3]
        current_candle = data.iloc[-2]

        current_sma_native = current_candle[sma_col]
        previous_close_native = previous_candle['Close']
        previous_sma_native = previous_candle[sma_col]

        # 4. LOG DE ANÁLISE NA MOEDA NATIVA (EX: SOL)
        logger.info(f"Análise ({pair_details['quote_symbol']}): Preço Real-Time {real_time_price_native:.8f} | Média da Última Vela {current_sma_native:.8f}")

        # 5. CONVERSÃO E LOG EM USD
        is_sol_pair = pair_details['quote_symbol'] in ['SOL', 'WSOL']
        quote_price_usd = 1.0 # Padrão para pares já em USD (ex: USDC)

        if is_sol_pair:
            sol_price_usd = None
            if real_time_price_usd and real_time_price_native > 0:
                sol_price_usd = real_time_price_usd / real_time_price_native
            
            if not sol_price_usd: # Fallback se o cálculo falhar
                logger.warning("Não foi possível calcular o preço do SOL. Usando par de fallback SOL/USDC...")
                sol_price_data = await fetch_dexscreener_prices(SOL_USDC_PAIR_ADDRESS)
                if sol_price_data and sol_price_data['pair_price_usd']:
                    sol_price_usd = sol_price_data['pair_price_usd']

            if not sol_price_usd:
                 logger.error("Falha CRÍTICA ao obter preço do SOL para conversão do log.")
                 await send_telegram_message("⚠️ Não foi possível converter a Média Móvel para USD para o log.")
                 return
            
            quote_price_usd = sol_price_usd
            
        current_sma_usd = current_sma_native * quote_price_usd
        
        # LOG DE ANÁLISE EM USD
        logger.info(f"Análise (USD): Preço Real-Time ${real_time_price_usd:.8f} | Média da Última Vela ${current_sma_usd:.8f}")

        # 6. Lógica de Decisão
        if in_position:
            stop_loss_price_usd = entry_price * (1 - stop_loss_percent / 100)
            logger.info(f"Posição aberta. Entrada(USD): ${entry_price:.8f}, Stop(USD): ${stop_loss_price_usd:.8f}")

            if real_time_price_usd <= stop_loss_price_usd:
                await execute_sell_order(reason=f"Stop-Loss atingido em ${stop_loss_price_usd:.8f}")
                return

            sell_signal = previous_close_native >= previous_sma_native and real_time_price_native < current_sma_native
            if sell_signal:
                await execute_sell_order(reason="Cruzamento de Média Móvel")
                return

        buy_signal = previous_close_native <= previous_sma_native and real_time_price_native > current_sma_native
        if not in_position and buy_signal:
            logger.info("Sinal de COMPRA detectado.")
            await execute_buy_order(amount, real_time_price_usd)

    except Exception as e:
        logger.error(f"Ocorreu um erro em check_strategy: {e}", exc_info=True)
        await send_telegram_message(f"⚠️ Erro inesperado na estratégia: {e}")

# --- Funções e Comandos do Telegram ---
async def send_telegram_message(message):
    if application:
        await application.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')

async def start(update, context):
    await update.effective_message.reply_text(
        'Olá! Sou seu bot de autotrade para a rede Solana.\n'
        'A análise é feita com histórico do **GeckoTerminal** e preço em tempo real do **Dexscreener**.\n'
        'A negociação é via **Jupiter**.\n\n'
        'Use o comando `/set` para configurar:\n'
        '`/set <ENDEREÇO_DO_TOKEN> <SÍMBOLO_COTAÇÃO> <TIMEFRAME> <MA> <VALOR> <STOP_%>`\n\n'
        '**Exemplo (WIF/SOL):**\n'
        '`/set EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzL7M6fV2zY2g6 SOL 1h 21 0.1 7`\n\n'
        '**Comandos:**\n'
        '• `/run` - Inicia o bot.\n'
        '• `/stop` - Para o bot.\n'
        '• `/buy` - Força uma compra.\n'
        '• `/sell` - Força uma venda.',
        parse_mode='Markdown'
    )

async def set_params(update, context):
    global parameters, check_interval_seconds
    if bot_running:
        await update.effective_message.reply_text("Pare o bot com /stop antes de alterar os parâmetros.")
        return
    try:
        base_token_contract = context.args[0]
        quote_symbol_input = context.args[1].upper()
        timeframe, ma_period = context.args[2].lower(), int(context.args[3])
        amount, stop_loss_percent = float(context.args[4]), float(context.args[5])

        interval_map = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
        if timeframe not in interval_map:
            await update.effective_message.reply_text(f"⚠️ Timeframe '{timeframe}' não suportado."); return
        check_interval_seconds = interval_map[timeframe]

        token_search_url = f"https://api.dexscreener.com/latest/dex/tokens/{base_token_contract}"
        async with httpx.AsyncClient() as client:
            response = await client.get(token_search_url)
            response.raise_for_status()
            token_res = response.json()

        if not token_res.get('pairs'):
            await update.effective_message.reply_text(f"⚠️ Nenhum par encontrado para o contrato."); return

        accepted_symbols = [quote_symbol_input]
        if quote_symbol_input == 'SOL': accepted_symbols.append('WSOL')
        
        valid_pairs = [p for p in token_res['pairs'] if p.get('quoteToken', {}).get('symbol') in accepted_symbols]
        if not valid_pairs:
            await update.effective_message.reply_text(f"⚠️ Nenhum par com `{quote_symbol_input}` encontrado."); return

        trade_pair = max(valid_pairs, key=lambda p: p.get('liquidity', {}).get('usd', 0))
        base_token_symbol = trade_pair['baseToken']['symbol'].lstrip('$')
        quote_token_symbol = trade_pair['quoteToken']['symbol']

        parameters = {
            "base_token_symbol": base_token_symbol, "quote_token_symbol": quote_token_symbol,
            "timeframe": timeframe, "ma_period": ma_period, "amount": amount,
            "stop_loss_percent": stop_loss_percent,
            "trade_pair_details": {
                "base_symbol": base_token_symbol, "quote_symbol": quote_token_symbol,
                "base_address": trade_pair['baseToken']['address'], "quote_address": trade_pair['quoteToken']['address'],
                "pair_address": trade_pair['pairAddress'],
                "quote_decimals": 9 if quote_token_symbol in ['SOL', 'WSOL'] else 6
            }
        }
        await update.effective_message.reply_text(
            f"✅ *Parâmetros definidos!*\n\n"
            f"📊 *Fonte:* `GeckoTerminal + Dexscreener`\n"
            f"🪙 *Par:* `{base_token_symbol}/{quote_token_symbol}`\n"
            f"⏰ *Timeframe:* `{timeframe}`\n"
            f"📈 *Média Móvel:* `{ma_period}` períodos\n"
            f"💰 *Valor/Ordem:* `{amount}` {quote_symbol_input}\n"
            f"📉 *Stop-Loss:* `{stop_loss_percent}%`",
            parse_mode='Markdown'
        )
    except (IndexError, ValueError):
        await update.effective_message.reply_text(
            "⚠️ *Formato incorreto.*\n"
            "Use: `/set <CONTRATO> <COTAÇÃO> <TIMEFRAME> <MA> <VALOR> <STOP_%>`\n",
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Erro em set_params: {e}")
        await update.effective_message.reply_text(f"⚠️ Erro ao configurar: {e}")

async def run_bot(update, context):
    global bot_running, periodic_task
    if not all(p is not None for p in parameters.values() if p != parameters['trade_pair_details']):
        await update.effective_message.reply_text("Defina os parâmetros com /set primeiro."); return
    if bot_running:
        await update.effective_message.reply_text("O bot já está em execução."); return
    bot_running = True
    logger.info("Bot de trade iniciado.")
    await update.effective_message.reply_text("🚀 Bot iniciado! Verificando a estratégia...")
    if periodic_task is None or periodic_task.done():
        periodic_task = asyncio.create_task(periodic_checker())
    await check_strategy()

async def stop_bot(update, context):
    global bot_running, in_position, entry_price, periodic_task
    if not bot_running:
        await update.effective_message.reply_text("O bot já está parado."); return
    bot_running = False
    if periodic_task:
        periodic_task.cancel()
        periodic_task = None
    in_position, entry_price = False, 0.0
    logger.info("Bot de trade parado.")
    await update.effective_message.reply_text("🛑 Bot parado. Posição e tarefas resetadas.")

async def manual_buy(update, context):
    if not bot_running: await update.effective_message.reply_text("O bot precisa estar rodando. Use /run."); return
    if in_position: await update.effective_message.reply_text("Já existe uma posição aberta. Venda com /sell."); return
    logger.info("Comando /buy recebido. Forçando compra...")
    await update.effective_message.reply_text("Forçando ordem de compra...")
    try:
        pair_details = parameters['trade_pair_details']
        price_data = await fetch_dexscreener_prices(pair_details['pair_address'])
        real_time_price_usd = price_data.get('pair_price_usd') if price_data else None
        if real_time_price_usd and real_time_price_usd > 0:
            await execute_buy_order(parameters["amount"], real_time_price_usd)
        else:
            raise ValueError("Preço em USD inválido ou nulo")
    except Exception as e:
        logger.error(f"Erro na compra manual: {e}")
        await update.effective_message.reply_text("⚠️ Não foi possível obter o preço para a compra manual.")

async def manual_sell(update, context):
    if not bot_running: await update.effective_message.reply_text("O bot precisa estar rodando. Use /run."); return
    if not in_position: await update.effective_message.reply_text("Nenhuma posição aberta para vender."); return
    logger.info("Comando /sell recebido. Forçando venda...")
    await update.effective_message.reply_text("Forçando ordem de venda...")
    await execute_sell_order()

# --- Loop Principal e Inicialização ---
async def periodic_checker():
    logger.info(f"Verificador periódico iniciado: intervalo de {check_interval_seconds}s.")
    while True:
        try:
            await asyncio.sleep(check_interval_seconds)
            if bot_running:
                logger.info("Executando verificação periódica da estratégia...")
                await check_strategy()
        except asyncio.CancelledError:
            logger.info("Verificador periódico cancelado."); break
        except Exception as e:
            logger.error(f"Erro no loop periódico: {e}")
            await asyncio.sleep(60) # Espera um pouco mais em caso de erro

def main():
    global application
    keep_alive()  # Inicia o servidor web
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set", set_params))
    application.add_handler(CommandHandler("run", run_bot))
    application.add_handler(CommandHandler("stop", stop_bot))
    application.add_handler(CommandHandler("buy", manual_buy))
    application.add_handler(CommandHandler("sell", manual_sell))

    logger.info("Bot do Telegram iniciado e aguardando comandos...")
    application.run_polling()

if __name__ == '__main__':
    main()
