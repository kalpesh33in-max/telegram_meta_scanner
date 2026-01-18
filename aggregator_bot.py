  1 import os
    2 import logging
    3 from telegram import Update
    4 from telegram.ext import Application, MessageHandler, filters, ContextTypes
    5
    6 # Enable logging
    7 logging.basicConfig(
    8     format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
    9 )
   10 logger = logging.getLogger(__name__)
   11
   12 # Get environment variables
   13 try:
   14     TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
   15 except KeyError as e:
   16     logger.critical(f"Critical Error: Environment variable {e} is not set or invalid.")
   17     raise SystemExit(f"Stopping bot. Please set a valid {e} environment variable.")
   18
   19
   20 async def catch_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
   21     """This function is called for every message the bot receives."""
   22
   23     # We are printing directly to the console/log to be as simple as possible.
   24     print("<<<<< MESSAGE RECEIVED! >>>>>")
   25     print(f"Chat ID: {update.effective_chat.id if update.effective_chat else 'N/A'}")
   26     print(f"Message Text: {update.effective_message.text if update.effective_message else 'N/A'}")
   27     print("<<<<< END OF MESSAGE >>>>>")
   28
   29     logger.info("<<<<< MESSAGE RECEIVED! >>>>>")
   30     logger.info(f"Chat ID: {update.effective_chat.id if update.effective_chat else 'N/A'}")
   31     logger.info(f"Message Text: {update.effective_message.text if update.effective_message else 'N/A'}")
   32     logger.info("<<<<< END OF MESSAGE >>>>>")
   33
   34
   35 def main() -> None:
   36     """Start the bot."""
   37     print("--- Starting Simple Test Bot ---")
   38
   39     application = Application.builder().token(TOKEN).build()
   40
   41     # This handler will react to ALL messages from ALL chats the bot is in.
   42     application.add_handler(MessageHandler(filters.ALL, catch_all_messages))
   43
   44     print("--- Bot is now polling for updates ---")
   45     # Start the Bot
   46     application.run_polling()
   47
   48
   49 if __name__ == "__main__":
   50     main()
