import logging
import os
import asyncio
from decimal import Decimal, getcontext
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from binance import Client
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv

# Initialisation
load_dotenv()
getcontext().prec = 8

# Configuration
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET')

# Vérification des variables
if not all([TELEGRAM_TOKEN, BINANCE_API_KEY, BINANCE_API_SECRET]):
    raise ValueError("Configurez toutes les variables dans .env")

# Clients
binance_client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
pending_orders = {}

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot_trading.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

async def get_asset_balance(asset: str) -> Decimal:
    """Récupère le solde disponible d'un actif"""
    try:
        balance = binance_client.get_asset_balance(asset=asset)
        return Decimal(balance['free'])
    except Exception as e:
        logger.error(f"Erreur lors de la récupération du solde {asset}: {e}")
        return Decimal(0)

async def get_all_balances():
    """Récupère tous les soldes non nuls avec leur valeur en USDT"""
    try:
        account = binance_client.get_account()
        balances = []
        
        for item in account['balances']:
            free = Decimal(item['free'])
            if free > Decimal('0.0001'):  # Ignore les soldes négligeables
                asset = item['asset']
                
                if asset in ['USDT', 'USDC']:
                    usdt_value = free
                else:
                    # Essaye USDT puis USDC comme paire de référence
                    for stablecoin in ['USDT', 'USDC']:
                        try:
                            ticker = f"{asset}{stablecoin}"
                            price = Decimal(binance_client.get_symbol_ticker(symbol=ticker)['price'])
                            usdt_value = free * price
                            break
                        except:
                            continue
                    else:
                        usdt_value = Decimal(0)
                
                balances.append({
                    'asset': asset,
                    'free': free,
                    'value': usdt_value
                })
        
        return sorted(balances, key=lambda x: -x['value'])  # Tri par valeur décroissante
    
    except Exception as e:
        logger.error(f"Erreur get_all_balances: {e}")
        return None

async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le portefeuille complet avec valeurs en USDT"""
    try:
        balances = await get_all_balances()
        if not balances:
            await update.message.reply_text("❌ Impossible de récupérer les soldes")
            return

        message = "💰 <b>VOTRE PORTEFEUILLE</b>\n\n"
        total = Decimal(0)
        
        for item in balances:
            amount = item['free'].normalize()
            message += f"• {item['asset']}: {amount}"
            
            if item['asset'] not in ['USDT', 'USDC']:
                value = item['value'].quantize(Decimal('0.01'))
                message += f" (≈{value} USDT)"
            
            message += "\n"
            total += item['value']

        total_usd = total.quantize(Decimal('0.01'))
        message += f"\n💵 <b>TOTAL ESTIMÉ</b>: {total_usd} USDT"
        
        await update.message.reply_text(message, parse_mode='HTML')
    
    except Exception as e:
        logger.error(f"Erreur show_balance: {e}")
        await update.message.reply_text("❌ Erreur lors de la récupération des soldes")

async def check_symbol_rules(pair: str):
    """Vérifie les règles de trading pour une paire"""
    try:
        info = binance_client.get_symbol_info(pair)
        if not info:
            return None
        
        lot_size = next(f for f in info['filters'] if f['filterType'] == 'LOT_SIZE')
        return {
            'min_qty': Decimal(lot_size['minQty']),
            'step_size': Decimal(lot_size['stepSize'])
        }
    except Exception as e:
        logger.error(f"Erreur check_symbol_rules {pair}: {e}")
        return None

async def execute_trade(pair: str, quantity: Decimal, is_buy: bool):
    """Exécute un ordre de marché"""
    try:
        rules = await check_symbol_rules(pair)
        if not rules:
            return None, "Règles de trading non disponibles"
        
        # Ajustement de la quantité selon les règles
        precision = abs(rules['step_size'].as_tuple().exponent)
        adjusted_qty = (quantity // rules['step_size']) * rules['step_size']
        adjusted_qty = adjusted_qty.quantize(Decimal(10) ** -precision)
        
        if adjusted_qty < rules['min_qty']:
            return None, f"Quantité trop faible. Minimum: {rules['min_qty']}"
        
        # Exécution de l'ordre
        if is_buy:
            order = binance_client.order_market_buy(
                symbol=pair,
                quantity=float(adjusted_qty))
        else:
            order = binance_client.order_market_sell(
                symbol=pair,
                quantity=float(adjusted_qty))
        
        return order, None
    
    except BinanceAPIException as e:
        return None, f"Erreur Binance: {e.message}"
    except Exception as e:
        return None, f"Erreur: {str(e)}"

async def confirm_trade(update: Update, context: ContextTypes.DEFAULT_TYPE, is_buy: bool):
    """Processus de confirmation pour achat/vente"""
    try:
        if len(context.args) != 2:
            cmd = "buy" if is_buy else "sell"
            await update.message.reply_text(
                f"Usage: /{cmd} <montant> <paire>\n"
                f"Ex: /{cmd} 100 BTCUSDT ou /{cmd} 0.5 ETHUSDC")
            return

        amount = Decimal(context.args[0])
        pair = context.args[1].upper()
        action = "ACHAT" if is_buy else "VENTE"

        # Validation de la paire
        if not any(pair.endswith(coin) for coin in ['USDT', 'USDC']):
            await update.message.reply_text("❌ Seules les paires USDT/USDC sont supportées")
            return

        if is_buy:
            # Calcul pour achat
            ticker = binance_client.get_symbol_ticker(symbol=pair)
            price = Decimal(ticker['price'])
            quantity = amount / price
        else:
            # Vérification pour vente
            asset = pair.replace('USDT', '').replace('USDC', '')
            balance = await get_asset_balance(asset)
            if amount > balance:
                await update.message.reply_text(f"❌ Solde insuffisant. Disponible: {balance} {asset}")
                return
            quantity = amount

        # Enregistrement temporaire
        pending_orders[update.effective_user.id] = {
            'pair': pair,
            'quantity': quantity,
            'is_buy': is_buy
        }

        # Préparation du message de confirmation
        ticker = binance_client.get_symbol_ticker(symbol=pair)
        price = Decimal(ticker['price'])
        total = quantity * price

        keyboard = [
            [InlineKeyboardButton(f"✅ Confirmer {action}", callback_data=f"confirm_{pair}")],
            [InlineKeyboardButton("❌ Annuler", callback_data="cancel")]
        ]

        await update.message.reply_text(
            f"📊 <b>CONFIRMATION {action}</b>\n\n"
            f"• Paire: {pair}\n"
            f"• Quantité: {quantity.normalize()}\n"
            f"• Prix actuel: {price} USD\n"
            f"• Montant total: {total.quantize(Decimal('0.01'))} USD",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )

    except ValueError:
        await update.message.reply_text("❌ Le montant doit être un nombre valide")
    except Exception as e:
        logger.error(f"Erreur confirm_trade: {e}")
        await update.message.reply_text(f"❌ Erreur: {str(e)}")

async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère la commande /buy"""
    await confirm_trade(update, context, is_buy=True)

async def sell_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère la commande /sell"""
    await confirm_trade(update, context, is_buy=False)

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère les interactions avec les boutons"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data.startswith("confirm_"):
        pair = data.split("_")[1]
        order_data = pending_orders.get(user_id)

        if order_data and order_data['pair'] == pair:
            await query.edit_message_text("🔄 Traitement en cours...")
            
            order, error = await execute_trade(
                pair=order_data['pair'],
                quantity=order_data['quantity'],
                is_buy=order_data['is_buy']
            )
            
            if order:
                action = "acheté" if order_data['is_buy'] else "vendu"
                fills = order.get('fills', [{}])
                message = (
                    f"✅ {order['symbol']} {action} avec succès!\n\n"
                    f"• Quantité: {order['executedQty']}\n"
                    f"• Prix moyen: {fills[0].get('price', 'N/A')}\n"
                    f"• Total: {order['cummulativeQuoteQty']} USD"
                )
            else:
                message = f"❌ {error}"

            await query.edit_message_text(message)
        else:
            await query.edit_message_text("❌ Opération expirée")

    elif data == "cancel":
        pending_orders.pop(user_id, None)
        await query.edit_message_text("❌ Opération annulée")

async def reset_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fonction améliorée pour effacer toute la conversation"""
    try:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        
        # Suppression des messages un par un
        async for message in context.bot.get_chat_history(chat_id):
            try:
                if message.message_id != update.message.message_id:  # Ne pas supprimer la commande /reset
                    await context.bot.delete_message(chat_id=chat_id, message_id=message.message_id)
                    await asyncio.sleep(0.2)  # Respect des limites de l'API
            except Exception as e:
                logger.warning(f"Impossible de supprimer le message {message.message_id}: {e}")
                continue
        
        # Suppression de la commande /reset après un délai
        await asyncio.sleep(1)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        except:
            pass
            
        # Nouveau message de démarrage
        await help_command(update, context)
        
    except Exception as e:
        logger.error(f"Erreur reset_conversation: {e}")
        await update.message.reply_text(
            "⚠️ Impossible de tout effacer automatiquement.\n"
            "Pour une réinitialisation complète:\n"
            "1. Allez dans les infos du chat\n"
            "2. Sélectionnez 'Effacer l'historique'\n"
            "3. Envoyez /start",
            reply_markup=ReplyKeyboardRemove()
        )

async def crypto_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le prix actuel d'une paire de trading"""
    try:
        if not context.args:
            await update.message.reply_text("Usage: /info <paire>\nEx: /info BTCUSDT")
            return

        pair = context.args[0].upper()
        
        # Vérification que la paire se termine par USDT ou USDC
        if not any(pair.endswith(coin) for coin in ['USDT', 'USDC']):
            await update.message.reply_text("❌ Seules les paires USDT/USDC sont supportées")
            return

        try:
            ticker = binance_client.get_symbol_ticker(symbol=pair)
            price = Decimal(ticker['price'])
            
            # Formatage du prix selon sa valeur
            if price > 100:
                price_str = f"{price.quantize(Decimal('0.01'))}"
            elif price > 1:
                price_str = f"{price.quantize(Decimal('0.0001'))}"
            else:
                price_str = f"{price.quantize(Decimal('0.000001'))}"
            
            await update.message.reply_text(
                f"📊 <b>PRIX ACTUEL</b>\n\n"
                f"• Paire: {pair}\n"
                f"• Prix: {price_str} USD",
                parse_mode='HTML'
            )
        except BinanceAPIException:
            await update.message.reply_text("❌ Paire introuvable. Vérifiez le format (ex: BTCUSDT)")
    except Exception as e:
        logger.error(f"Erreur crypto_info: {e}")
        await update.message.reply_text("❌ Erreur lors de la récupération du prix")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le message d'aide principal"""
    help_text = (
        "🤖 <b>BOT TRADING BINANCE</b>\n\n"
        "🔹 <b>Commandes disponibles</b>:\n\n"
        "• /buy montant paire - Acheter des cryptos\n"
        "• /sell montant paire - Vendre des cryptos\n"
        "• /balance - Afficher votre portefeuille\n"
        "• /info paire - Voir le prix d'une crypto\n"
        "• /reset - Réinitialiser toute la conversation\n"
        "• /help - Afficher ce message\n\n"
        "📌 <i>Exemples:</i>\n"
        "<code>/buy 100 BTCUSDT</code>\n"
        "<code>/sell 0.5 ETHUSDC</code>\n"
        "<code>/info SOLUSDT</code>\n\n"
        "🔒 Support USDT & USDC | Sécurisé"
    )
    await update.message.reply_text(help_text, parse_mode='HTML')

def main():
    """Configuration du bot"""
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Commandes
    app.add_handler(CommandHandler("start", help_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("balance", show_balance))
    app.add_handler(CommandHandler("buy", buy_command))
    app.add_handler(CommandHandler("sell", sell_command))
    app.add_handler(CommandHandler("info", crypto_info))
    app.add_handler(CommandHandler("reset", reset_conversation))
    app.add_handler(CallbackQueryHandler(handle_button))

    logger.info("Démarrage du bot...")
    app.run_polling()

if __name__ == "__main__":
    main()