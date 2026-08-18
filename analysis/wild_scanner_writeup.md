# In-the-wild evidence: a spoofed-identity crawler hit the tunnel

**9 sessions, 5f5d15db → 218ffbac, all within an 18-second window on 2026-08-18
16:21:48–16:22:06 UTC.** Tagged `label = 'wild_scanner_suspected'` in
`traversal.db` (was generic `unknown`) so they can never be confused with real
play data in any future query or report. Nothing was deleted.

## What we actually captured

Every session hit `/` (storefront root) with no `?label=` query param, via the
public Cloudflare quick-tunnel — not localhost. Pulled from each session's
`http_headers` and `page_load_signals` events:

- **User-Agent strings claim two different devices**: 3 sessions say `iPhone;
  CPU iPhone OS 15_0 ... Version/15.0 Mobile/15E148 Safari/604.1`; 6 say
  `Macintosh; Intel Mac OS X 10_15_7 ... Chrome/130.0.0.0`.
- **But every single one sends the identical Client Hint**: `sec-ch-ua:
  "Chromium";v="131", "Not_A Brand";v="24"` — including the ones claiming to be
  an iPhone. A real iPhone browser (Chrome-on-iOS is a WebKit wrapper, forced
  by Apple's App Store rules) does not emit desktop-style Chromium version
  Client Hints. A real device also can't be an iPhone and a Mac at the same
  time. **One underlying engine, multiple spoofed identities — this is the
  parallelism/inconsistency signal the whole product thesis is built on,
  caught live and unprompted, not staged.**
- **`sec-ch-ua-platform` doesn't even match its own User-Agent claim**:
  sessions report `"iOS"` or `"macOS"` — consistent with each other but still
  just a second layer of the same spoofed header set, not independent
  confirmation.
- **Software-rendered WebGL**: `ANGLE (Google, Vulkan 1.3.0 (SwiftShader
  Device (Subzero) (0x0000C0DE)), SwiftShader driver)` on every session that
  sent `page_load_signals` — the same software-rasterizer signature
  `webdriver_artifacts.py` already flags for raw CDP automation.
- **`navigator.webdriver` explicitly reports `false`** — notable specifically
  *because* a genuinely unremarkable browser doesn't need to actively assert
  this; patching it to `false` is itself a common automation-evasion step.
- **Behavior pattern**: repeatedly clicking the identical viewport coordinate
  (`{1802, 24}` desktop / `{862, 24}` and `{981, 24}` mobile — the nav link
  position in the top-right header) targeting an `<a>` element, then
  navigating between `/` and `/login` in a tight, repetitive loop. Classic
  systematic link-discovery behavior, not human browsing.
- **Burst timing**: 8-9 sessions in 18 seconds is not organic traffic to a
  URL that was never shared publicly — it's a scan or crawl that found the
  tunnel subdomain.

## What we do NOT have (being honest about the gap)

`cf-connecting-ip` and `x-forwarded-for` header **names** are present in
`header_order` (proof the request went through Cloudflare's edge, which
injects them), but their **values were never captured** —
`HEADER_FINGERPRINT_KEYS` in `server.py` only stores a handful of specific
header values (`accept`, `sec-ch-ua`, etc.), not IP-carrying ones. So: no
actual IP address, and no way to confirm whether all 9 sessions came from one
IP or several. If this capability matters going forward, `HEADER_FINGERPRINT_KEYS`
would need `cf-connecting-ip` added — not done here, since this pass is fixes
only, not new capture surface.

## Why this matters for the pitch

This isn't a synthetic demo of the detection thesis — it's the *product's own
stated core signal* (cross-request identity inconsistency) showing up
spontaneously, in real traffic, against test infrastructure that was never
advertised anywhere, within hours of being reachable. That's a much stronger
existence proof than anything the arcade's own generator scripts can produce,
because nobody built this scanner to prove a point — it's just what's already
crawling the internet.
