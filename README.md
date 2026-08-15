# herdr-remote-phone

Mobile-first fork of [dcolinmorgan/herdr-remote](https://github.com/dcolinmorgan/herdr-remote) with phone UX improvements for multi-machine Herdr control.

Upstream still owns the menu-bar app / Telegram relay core. This fork focuses on the **phone web UI + relay fields** that make remote agent control usable on iPhone.

## Why this fork

Daily remote control of several Herdr agents on more than one Mac needs:

- fixed machine profiles (Pro / Air), not a temporary trycloudflare URL
- conversation-first reading (not a raw terminal dump)
- phone-friendly compose / history / model switch

## Kun phone improvements

### Multi-machine
- Top **Pro / Air** profile switch
- Per-machine host + token (localStorage)
- Clear **offline** state when a machine is unreachable
- Safe reconnect (no stale websocket flipping the wrong profile offline)

### Conversation UX (`只看回答`)
- Highlight **你** / **Agent** messages separately
- Collapse long commands/code into fold rows
- Hide warning/status chrome from the main transcript
- Parse Pi **Tasks/TODO** trees out of chat into a bottom **TODO** chip + sheet
- Parse model / context / path into a bottom status bar (not mixed into chat)

### Phone controls
- Large **发送** button + textarea compose
- **历史**: pick a previous user message, stage edit (Esc Esc-aligned), resend
- **模型**: searchable model picker, sends one exact `/model provider/id`
- **/** command palette with full Pi built-ins + common extension commands (searchable)
- Tab **labels** from `herdr tab list` shown in the agent list

### Relay
- `tab_label` joined from `herdr tab list` so renamed tabs surface on the phone

## Install

Same general flow as upstream. Minimal phone path:

```bash
git clone https://github.com/kkunkunya/herdr-remote-phone.git
cd herdr-remote-phone/relay
# start relay (or install-service.sh for LaunchAgent)
./start.sh
```

Or use named Cloudflare tunnels and open:

```text
https://<your-host>/?token=<relay-token>
```

Pro / Air use the built-in fixed profiles and connect immediately; Settings remains available for an override.

Upstream install docs still apply for Herdi menu bar / Telegram / herdr-push:

- Upstream README: [dcolinmorgan/herdr-remote](https://github.com/dcolinmorgan/herdr-remote)
- Demo: [herdr-demo.pages.dev](https://herdr-demo.pages.dev)
- Push plugin: [dcolinmorgan/herdr-push](https://github.com/dcolinmorgan/herdr-push)
- Quick start remains in `QUICKSTART.md`

## Security notes

- Relay token grants terminal control. Keep tunnels authenticated.
- This deployment ships its relay token in both built-in profiles; anyone who can fetch this public page can control its relays.
- Model list in the UI is baked from the machine that built/deployed the page (`pi --list-models`). Refresh the page deploy after big model-catalog changes.

## Upstream

Forked from Apache-2.0 **herdr-remote** by dcolinmorgan.

Please keep contributing generic relay fixes upstream when possible; keep phone-opinionated UX here.

## License

Apache-2.0 (same as upstream). See `LICENSE`.
