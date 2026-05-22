# dietly-cli

A CLI tool for scraping diet catering offers from [dietly.pl](https://dietly.pl) for a given Polish city. Helps compare current prices, promo codes and length-based discounts so you can pick the best deal each month.

Uses the public, unauthenticated JSON API the dietly.pl frontend itself talks to. No headless browser, no HTML parsing.

## Features

- Filter by calorie values, meals per day, diet tag (`dietTag`), with an opt-in for "Wybór menu" (custom-menu) diets.
- Base price + price after the catering's active promo code, per day.
- Every length-based discount tier (PERCENTAGE and TOTAL) expanded into its own CSV row, **excluding tiers that don't beat the active promo** (the two don't stack — the larger discount wins).
- Per-day delivery fee, when the catering exposes it via the API.
- Two run modes: scripted (CLI flags) and interactive (a `questionary` wizard).

## Requirements

- Python ≥ 3.11
- [`uv`](https://docs.astral.sh/uv/) for venv and dependency management

## Usage

### Interactive wizard

Run with no arguments:

```bash
uv run dietly-cli
```

The wizard walks you through: city → calorie values → meals per day → diet types → whether to include "Wybór menu". Nothing is pre-selected; leave a checkbox prompt empty for "no filter on this dimension".

### Scripted (flags)

Pass `--city` plus any filter flags. **No filters are applied by default** — every flag you add narrows the result set.

```bash
uv run dietly-cli --city warszawa --calories 2000 3000 --meals 5 --diet-tags STANDARD
```

Full flag reference:

| Flag | Default | Description |
|---|---|---|
| `--city SLUG` | (required) | City slug, e.g. `wroclaw`, `warszawa`, `krakow` |
| `--calories KCAL [KCAL ...]` | no filter | Allowed calorie values |
| `--meals N` | no filter | Required meals per day |
| `--diet-tags TAG [TAG ...]` | no filter | Allowed diet tags (see list below) |
| `--include-menu-config` | off | Include "Wybór menu" diets (excluded by default) |
| `--output / -o PATH` | `dietly_<city>_<date>.csv` | Output CSV path |
| `--workers N` | `5` | Parallel HTTP workers |
| `--delay SEC` | `0.2` | Per-worker delay between API calls |
| `--page-size N` | `15` | Listing API page size |

Known `--diet-tags` values: `STANDARD`, `VEGETARIAN`, `VEGAN`, `VEGE AND FISH`, `GLUTEN FREE`, `GLUTEN LACTOSE FREE`, `KETO`, `LOW IG`, `HASHI`, `WEIGHT LOSS`, `SAMURAI`.

### Examples

```bash
# 2000+3000 kcal, 5 meals, standard diet only, in Warsaw
uv run dietly-cli --city warszawa \
    --calories 2000 3000 --meals 5 --diet-tags STANDARD

# Vegetarian and vegan diets, 1500 kcal, 4 meals, Kraków
uv run dietly-cli --city krakow \
    --diet-tags VEGETARIAN VEGAN --calories 1500 --meals 4

# Everything for Gdańsk, including "Wybór menu" — large output
uv run dietly-cli --city gdansk --include-menu-config
```

## CSV schema

Each row is a (catering × diet × option × calorie × tier) combination. The "base" row (no tier) has empty `tier_*` columns and represents the price with no length commitment.

Columns are grouped — identification first, then specs, then promo metadata, with the **comparable price columns at the very end** so you can sort/filter by them easily:

| Column | Description |
|---|---|
| `city` | City slug as passed to the script |
| `catering_fullname` | Display name of the catering |
| `rate` / `number_of_rates` | Average rating and number of ratings |
| `price_category` | `CHEAP` / `AVERAGE` / `EXPENSIVE` (dietly's own classification) |
| `delivery_fee_pln` | Per-day delivery fee, when published |
| `diet_name` / `diet_tag` | Diet display name + canonical tag |
| `diet_option_name` | Variant name (e.g. "5 posiłków") |
| `calories` / `meals_count` | Calories and meals per day |
| `tier_min_days` | Minimum days required to qualify for this tier |
| `tier_discount_value` / `tier_discount_type` | Discount magnitude and type (`PERCENTAGE` or `TOTAL` PLN) |
| `promo_discount_pct` / `promo_code` / `promo_date_to` | Active promo code details |
| `base_price_per_day_pln` | Base per-day price |
| `promo_price_per_day_pln` | Per-day price after the active promo code |
| `tier_final_price_pln` | Per-day price after the tier discount (computed from base, never stacked with the promo) |

## Legal and ethical notes

- **Personal use only.** dietly.pl's terms of service (§7) restrict platform use to personal use and protect content under copyright. Don't republish the resulting CSV or use the data commercially.
- The API is public but that's not blanket consent for arbitrary use. The defaults `--workers 5 --delay 0.2` keep the request rate polite — **don't crank these up** without reason.
- Prices may be stale. Always verify on the website before placing an order.

## License

[MIT](LICENSE)
