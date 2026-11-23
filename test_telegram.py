"""
AutoApply - Quick Telegram Integration Test Script
Sends a live test notification to verify bot token and chat ID.
"""
import asyncio
import httpx
from config import settings


async def send_test_notification():
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_CHAT_ID

    print("--- AutoApply Telegram Live Test ---")
    if not token or token == "CHANGE_ME_REVOKE_OLD_TOKEN":
        print("❌ Error: TELEGRAM_BOT_TOKEN is not set in .env")
        print("1. Open Telegram and search for @BotFather")
        print("2. Send /newbot and follow prompts to get your Bot Token")
        print("3. Add TELEGRAM_BOT_TOKEN=<your_token> in .env")
        return

    if not chat_id:
        print("❌ Error: TELEGRAM_CHAT_ID is not set in .env")
        print("1. Open Telegram and search for @userinfobot (or send a message to your new bot)")
        print("2. Get your numeric user ID")
        print("3. Add TELEGRAM_CHAT_ID=<your_chat_id> in .env")
        return

    message = (
        "🤖 *AutoApply Job Scout is Connected!*\n\n"
        "✅ Live alerts are active.\n"
        "• Auto-submit status receipts\n"
        "• Verification code fallbacks\n"
        "• Pre-submit validation alerts\n\n"
        "Happy job scouting!"
    )

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, json=payload, timeout=10.0)
            if res.status_code == 200:
                print("✅ SUCCESS! Test message sent to your Telegram.")
                print(f"Telegram API response: {res.json().get('ok')}")
            else:
                print(f"❌ Failed to send. Status: {res.status_code}")
                print(f"Response: {res.text}")
    except Exception as e:
        print(f"❌ Error communicating with Telegram API: {e}")


if __name__ == "__main__":
    asyncio.run(send_test_notification())
