# Standalone Post Stats deployment

This branch adds a separate service for historical Telegram post analytics. It does not
change the existing summary bot or its Dockerfile.

1. Create a new Railway service from this branch.
2. In that service's build settings, set the Dockerfile path to `Dockerfile.poststats`.
3. Add a Railway Volume mounted at `/data`.
4. Open the service domain at `/setup`.
5. Copy the one-time setup code from that service's Railway logs. On the page, enter it
   with the owner’s Telegram API ID, API hash, and phone number. Telegram sends its usual
   login code; two-step verification is supported too.
6. The browser opens `/poststats` after setup. Enter the owner’s group/channel in the
   existing chat field and load the chosen date range.

The connected Telegram account and generated page access token are stored only on that
service’s `/data` volume. There is no OpenAI key, BotFather token, shared Telegram
session, or `LISTENER_ALLOWED_CHATS` setting.

BotFather cannot replace the Telegram account login here: Telegram does not expose the
historical views, forwards, reactions, and comment counts used by `/poststats` to bots.
