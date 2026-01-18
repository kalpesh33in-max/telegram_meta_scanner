import os
import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

try:
    TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
except KeyError as e:
    logger.critical(f"Critical Error: Environment variable {e} is not set or invalid.")
    raise SystemExit(f"Stopping bot. Please set a valid {e} environment variable.")


async def catch_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("<<<<< MESSAGE RECEIVED! >>>>>")
    print(f"Chat ID: {update.effective_chat.id if update.effective_chat else 'N/A'}")
    print(f"Message Text: {update.effective_message.text if update.effective_message else 'N/A'}")
    print("<<<<< END OF MESSAGE >>>>>")
    
    logger.info("<<<<< MESSAGE RECEIVED! >>>>>")
    logger.info(f"Chat ID: {update.effective_chat.id if update.effective_chat else 'N/A'}")
    logger.info(f"Message Text: {update.effective_message.text if update.effective_message else 'N/A'}")
    logger.info("<<<<< END OF MESSAGE >>>>>")


def main() -> None:
    print("--- Starting Simple Test Bot ---")
    
    application = Application.builder().token(TOKEN).build()

    application.add_handler(MessageHandler(filters.ALL, catch_all_messages))

    print("--- Bot is now polling for updates ---")
    application.run_polling()


if __name__ == "__main__":
    main()
