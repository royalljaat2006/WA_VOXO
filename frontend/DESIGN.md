# Design

The UI follows **Eko Factory Design Language v1.0** (`eko-factory.tokens.json`,
copied here from the kit).

`src/eko-factory.css` is generated from those tokens. When the kit's own
`eko-factory.css` arrives, **replace that file wholesale** rather than editing
it — the design system should stay the single source, and this a faithful copy.

## Two palettes, on purpose

The chat column keeps **WhatsApp's** colours. The point of that column is "this
is what it looks like on a phone", and restyling it in eko's brand would make the
demo look like a product that does not exist.

Everything around it — header, status, trace, mission control — is **Eko
Factory**. That is our tooling, and it should look like eko's.

## Semantic families do real work

The kit defines four: `verified · done`, `running · notice`, `parked · stale`,
`failed · rejected`. Mission control's event vocabulary maps onto them one for
one, so the audit table needs no colours of its own:

| Event | Family |
|---|---|
| `transfer_executed` | verified |
| `intent_prepared` | running |
| `pin_attempt_failed`, `intent_cancelled`, `intent_expired` | parked |
| `transfer_failed`, `transfer_refused` | failed |

Brand orange appears once, as the rule beside the title. On a screen whose job is
to show state, brand colour competing with the semantic families would make
neither readable.

## Fonts are bundled, not fetched

IBM Plex Sans and Mono ship as npm packages. A demo must not depend on the
venue's wifi, and web fonts are the first thing a bad connection drops.

Digits use `font-variant-numeric: tabular-nums` wherever money or ids appear, so
columns line up and a changing amount does not make the row jitter.

## The kit's four principles

They are the same ones this codebase already argues for, which is why adopting it
was a re-skin rather than a redesign:

- **Local-first** — the PIN gate runs in our process, never in a model
- **Provider-neutral** — `PaymentGateway` and `CampaignDataSource` are boundaries
- **Human-controlled** — a model may propose a transfer; only a PIN executes one
- **Evidence-backed** — the append-only audit trail, and mission control reading
  from it rather than from a parallel tally
