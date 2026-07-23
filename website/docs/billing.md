---
id: billing
title: Billing
sidebar_position: 4
---

# Billing

Herald has two billing modes: **Credits** (Herald manages everything) and **BYOK** (Bring Your Own Key). Both plans include a free tier of 20 bursts per month — no credit card required to start.

---

## What is a "burst"?

A burst is one complete push-notified voice exchange:

1. Herald sends you a push notification.
2. You tap and record a voice reply.
3. Herald transcribes, processes, and speaks the response back.

One notification + one reply = **one burst**. If you don't reply to a notification (e.g. you just read the summary text), no burst is consumed.

---

## Plans at a glance

|  | **Credits** | **BYOK** |
|---|---|---|
| Gemini API | Herald provides | You provide |
| Relay fee | Included | $2 / month |
| Per burst cost | ~$0.05 | Your API cost only |
| Free tier | 20 bursts / month | 20 bursts / month |
| Best for | Everyone — no setup | Developers with existing API quota |
| Requires credit card | Only to go above free tier | Only for relay fee |

---

## Credits mode

Credits is the default mode. Herald bundles a managed Gemini API key and charges a flat per-burst rate of approximately **$0.05** per burst.

### What's included in $0.05?

| Component | Approximate cost |
|---|---|
| STT (Gemini transcription, ~10s audio) | $0.006 |
| LLM inference (Gemini 1.5 Flash, ~800 tokens) | $0.001 |
| TTS (Gemini TTS, ~150 words response) | $0.004 |
| Relay infrastructure | $0.008 |
| Margin + overhead | $0.031 |
| **Total** | **~$0.050** |

### Free tier

Your first 20 bursts each calendar month are completely free — no credit card, no trial expiry. Limits reset on the 1st of each month.

### Adding credits

To go beyond the free tier, add a payment method in the Herald app under **Settings → Billing → Add Credits**. Credits are pre-purchased in blocks:

| Block | Price | Per burst |
|---|---|---|
| Starter — 50 bursts | $2.50 | $0.050 |
| Standard — 250 bursts | $11.00 | $0.044 |
| Power — 1,000 bursts | $40.00 | $0.040 |

Unused credits roll over indefinitely. There is no subscription — you pay only when you top up.

### Enabling Credits mode

Credits is the default. Your config only needs:

```yaml
plugins:
  herald-relay:
    device_token: "your-token-here"
    # mode: credits  ← this is the default, you can omit it
```

---

## BYOK mode (Bring Your Own Key)

In BYOK mode, you supply your own Google AI Studio (Gemini) API key. Herald charges only a flat **$2/month** relay fee to keep your outbound WebSocket connection alive and route push notifications.

### Why BYOK?

- You already have Gemini API quota (e.g. from a Google Cloud project).
- You want full cost visibility — every API call appears in your Google Cloud billing dashboard.
- You're a power user who wants to choose your own Gemini model tier.

### BYOK pricing math

A typical burst with Gemini 1.5 Flash costs about:

```
STT:  10s audio  → ~$0.006
LLM:  800 tokens → ~$0.001
TTS:  150 words  → ~$0.004
──────────────────────────
Total per burst:    ~$0.011
```

At 50 bursts/month: **$0.55 in API + $2 relay = $2.55 total** — cheaper than Credits for heavy users.

At 10 bursts/month: **$0.11 in API + $2 relay = $2.11 total** — more expensive than Credits ($0.50). Credits is better for casual users.

**Break-even: ~45 bursts/month.** If you do more than 45 bursts/month, BYOK saves money.

### Getting a Gemini API key

1. Go to [Google AI Studio](https://aistudio.google.com/app/apikey).
2. Click **Create API Key**.
3. Copy the key (starts with `AIzaSy`).

### Enabling BYOK mode

```yaml
plugins:
  herald-relay:
    device_token: "your-token-here"
    mode: byok
    gemini_api_key: "AIzaSyYOUR_KEY_HERE"
```

### Paying the relay fee

The $2/month relay fee is billed through the Herald app under **Settings → Billing → BYOK Subscription**. You can cancel at any time; cancellation takes effect at the end of the current billing period.

---

## Checking your usage

At any time, run:

```bash
hermes herald status
```

Output:

```
Herald Plugin Status
────────────────────
Mode:          credits
Device:        iPhone 15 Pro (••••a3f2)
Relay:         connected (us-east)

This month:
  Bursts used:   12 / 20 (free tier)
  Bursts remain: 8 free, then ~$0.05 each
  Paid credits:  $5.00 balance (~100 bursts)

Billing cycle resets: Aug 1, 2025
```

---

## FAQ

**Do notifications count as bursts?**
No. You're only charged when you tap a notification and record a voice reply. Simply reading the notification text is free.

**What happens when I run out of credits?**
Herald will still send push notifications and display the text summary. Voice burst replies will be disabled until you add more credits. You won't be charged unexpectedly.

**Can I switch between modes?**
Yes. Change `mode:` in your config and restart Hermes. Switching to BYOK cancels any unused Credits balance (we'll prorate a refund). Switching from BYOK cancels your relay subscription at the end of the billing period.

**Is there a per-month cap?**
Not by default. You can set a monthly burst cap in the Herald app under **Settings → Billing → Spend Limits** to prevent runaway costs.
