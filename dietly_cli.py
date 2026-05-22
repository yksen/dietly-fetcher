#!/usr/bin/env python3
"""Scrape diet prices from dietly.pl for a given Polish city.

Run with `--city <slug>` (and optional filter flags) for scripted mode, or
run without arguments to enter the interactive wizard.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import questionary
import requests
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util.retry import Retry

API_BASE = "https://dietly.pl/api"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

LISTING_SORTS = ("default", "awarded-and-top")

POPULAR_CITIES: list[tuple[str, str]] = [
    ("Wrocław", "wroclaw"),
    ("Warszawa", "warszawa"),
    ("Kraków", "krakow"),
    ("Poznań", "poznan"),
    ("Gdańsk", "gdansk"),
    ("Łódź", "lodz"),
    ("Katowice", "katowice"),
    ("Szczecin", "szczecin"),
    ("Białystok", "bialystok"),
    ("Toruń", "torun"),
    ("Gdynia", "gdynia"),
]

KNOWN_DIET_TAGS: list[str] = [
    "STANDARD",
    "VEGETARIAN",
    "VEGAN",
    "VEGE AND FISH",
    "GLUTEN FREE",
    "GLUTEN LACTOSE FREE",
    "KETO",
    "LOW IG",
    "HASHI",
    "WEIGHT LOSS",
    "SAMURAI",
]

COMMON_CALORIES: list[int] = [1000, 1200, 1500, 1800, 2000, 2500, 3000, 3500]

CSV_COLUMNS = [
    "city",
    "catering_fullname",
    "rate",
    "number_of_rates",
    "price_category",
    "delivery_fee_pln",
    "diet_name",
    "diet_tag",
    "diet_option_name",
    "calories",
    "meals_count",
    "tier_min_days",
    "tier_discount_value",
    "tier_discount_type",
    "promo_discount_pct",
    "promo_code",
    "promo_date_to",
    "base_price_per_day_pln",
    "promo_price_per_day_pln",
    "tier_final_price_pln",
]


@dataclass
class FilterConfig:
    """All filters applied to (diet, option, calories) combinations.

    None means "no filter on this dimension"."""

    diet_tags: set[str] | None
    calories: set[int] | None
    meals_count: int | None
    include_menu_config: bool


@dataclass
class RunConfig:
    city: str
    output: str
    workers: int
    delay: float
    page_size: int
    filters: FilterConfig = field(default_factory=lambda: FilterConfig(None, None, None, False))


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=6,
        backoff_factor=0.8,
        status_forcelist=(403, 429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


def fetch_listing_page(
    session: requests.Session, sort: str, city: str, page: int, page_size: int
) -> dict[str, Any]:
    url = f"{API_BASE}/open/search/full/{sort}"
    params = {"cN": city, "page": page, "pageSize": page_size, "rV": "V2023_1"}
    r = session.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_all_listings(
    session: requests.Session, city: str, page_size: int
) -> tuple[list[dict[str, Any]], int | None]:
    """Merge results from multiple sort variants — `awarded-and-top` silently
    omits some non-awarded caterings, while `default` returns a different (but
    overlapping) subset. Dedupe by `name` (slug)."""
    by_slug: dict[str, dict[str, Any]] = {}
    city_id: int | None = None
    for sort in LISTING_SORTS:
        first = fetch_listing_page(session, sort, city, 1, page_size)
        if city_id is None:
            city_obj = first.get("city") or {}
            city_id = city_obj.get("cityId")
        total_pages = int(first.get("totalPages", 1))
        for c in first.get("searchData", []):
            by_slug.setdefault(c["name"], c)
        for page in range(2, total_pages + 1):
            data = fetch_listing_page(session, sort, city, page, page_size)
            for c in data.get("searchData", []):
                by_slug.setdefault(c["name"], c)
    return list(by_slug.values()), city_id


def fetch_sector_info(
    session: requests.Session, city_id: int, slug: str
) -> tuple[int | None, float | None]:
    """Return (sectorId, deliveryFee). deliveryFee is per-day in PLN, often null."""
    url = f"{API_BASE}/dietly/open/cities/id"
    headers = {"api-key": "123", "company-id": slug}
    r = session.get(url, params={"cityIds": city_id}, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list) or not data:
        return None, None
    entry = next(
        (e for e in data if e.get("cityId") == city_id and e.get("sectorId") is not None),
        data[0],
    )
    sid = entry.get("sectorId")
    fee = entry.get("deliveryFee")
    return (
        int(sid) if sid is not None else None,
        float(fee) if fee is not None else None,
    )


def fetch_diet_calories_details(
    session: requests.Session, slug: str
) -> list[dict[str, Any]]:
    url = f"{API_BASE}/open/company-details/{slug}/dietCaloriesDetails"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def fetch_diet_details(
    session: requests.Session, slug: str
) -> list[dict[str, Any]]:
    url = f"{API_BASE}/open/company-details/{slug}/dietDetails"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def discounts_by_diet_id(
    diet_details: list[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    for d in diet_details:
        did = d.get("dietId") or d.get("id")
        if did is None:
            continue
        discs = d.get("discounts") or []
        if discs:
            out[int(did)] = discs
    return out


def price_for_sector(
    prices: list[dict[str, Any]], sector_id: int
) -> float | None:
    for p in prices or []:
        if p.get("sectorId") == sector_id and p.get("price") is not None:
            return float(p["price"])
    return None


def compute_promo_price(base: float, promo: dict[str, Any] | None) -> float | None:
    if not promo:
        return None
    pct = promo.get("discountPercents")
    if pct is None:
        return None
    try:
        return round(base * (1 - float(pct) / 100.0), 2)
    except (TypeError, ValueError):
        return None


def apply_tier_discount(effective: float, value: float, dtype: str) -> float:
    if dtype == "PERCENTAGE":
        return round(effective * (1 - value / 100.0), 2)
    if dtype == "TOTAL":
        return round(effective - value, 2)
    return round(effective, 2)


def tier_rows(
    base_row: dict[str, Any],
    discounts: list[dict[str, Any]],
    base_price: float,
    promo_price: float | None,
) -> list[dict[str, Any]]:
    """Length-based tier discounts do NOT stack with the promo code; whichever
    is larger applies. So tier_final_price is computed off base_price, and we
    drop tiers that don't beat promo_price."""
    rows = [{**base_row, "tier_min_days": "", "tier_discount_value": "", "tier_discount_type": "", "tier_final_price_pln": ""}]
    for d in sorted(discounts or [], key=lambda x: x.get("minimumDays") or 0):
        min_days = d.get("minimumDays")
        value = d.get("discount")
        dtype = d.get("discountType", "")
        if min_days is None or value is None:
            continue
        tier_price = apply_tier_discount(base_price, float(value), dtype)
        if promo_price is not None and tier_price >= promo_price:
            continue
        rows.append({
            **base_row,
            "tier_min_days": min_days,
            "tier_discount_value": float(value),
            "tier_discount_type": dtype,
            "tier_final_price_pln": tier_price,
        })
    return rows


def is_wybor_menu(entry: dict[str, Any]) -> bool:
    if entry.get("menuConfiguration") is True:
        return True
    name = (entry.get("dietName") or "").lower()
    return "wyb" in name and "menu" in name


def keep_entry(entry: dict[str, Any], cfg: FilterConfig) -> bool:
    if cfg.diet_tags is not None and entry.get("dietTag") not in cfg.diet_tags:
        return False
    if cfg.calories is not None and entry.get("calories") not in cfg.calories:
        return False
    if cfg.meals_count is not None:
        meals = entry.get("meals") or []
        if len(meals) != cfg.meals_count:
            return False
    if not cfg.include_menu_config and is_wybor_menu(entry):
        return False
    return True


def process_catering(
    session: requests.Session,
    city: str,
    city_id: int,
    catering: dict[str, Any],
    delay: float,
    filters: FilterConfig,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return (rows, skip_reason). skip_reason is None when at least 1 row produced or
    when no rows matched but catering was reachable."""
    slug = catering.get("name")
    if not slug:
        return [], "no_slug"

    time.sleep(delay)
    try:
        sector_id, delivery_fee = fetch_sector_info(session, city_id, slug)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "err"
        return [], f"sector_http_{code}"
    except requests.RequestException:
        return [], "sector_network_error"

    if sector_id is None:
        return [], "no_sector_for_city"

    time.sleep(delay)
    try:
        dcd = fetch_diet_calories_details(session, slug)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "err"
        return [], f"dcd_http_{code}"
    except requests.RequestException:
        return [], "dcd_network_error"

    discounts_map: dict[int, list[dict[str, Any]]] = {}
    needs_diet_details = any(
        keep_entry(e, filters) and not e.get("discounts") for e in dcd
    )
    if needs_diet_details:
        time.sleep(delay)
        try:
            dd = fetch_diet_details(session, slug)
            discounts_map = discounts_by_diet_id(dd)
        except requests.RequestException:
            discounts_map = {}

    promo = catering.get("activePromotion")
    rows: list[dict[str, Any]] = []
    for entry in dcd:
        if not keep_entry(entry, filters):
            continue

        base = price_for_sector(entry.get("prices") or [], sector_id)
        if base is None:
            continue

        promo_price = compute_promo_price(base, promo)

        discounts = entry.get("discounts") or []
        if not discounts:
            did = entry.get("dietId")
            if did is not None:
                discounts = discounts_map.get(int(did), [])

        base_row = {
            "city": city,
            "catering_fullname": catering.get("fullName"),
            "rate": catering.get("rate"),
            "number_of_rates": catering.get("numberOfRates"),
            "price_category": catering.get("priceCategory"),
            "delivery_fee_pln": delivery_fee if delivery_fee is not None else "",
            "diet_name": entry.get("dietName"),
            "diet_tag": entry.get("dietTag"),
            "diet_option_name": entry.get("dietOptionName"),
            "calories": entry.get("calories"),
            "meals_count": len(entry.get("meals") or []),
            "promo_discount_pct": (promo or {}).get("discountPercents", ""),
            "promo_code": (promo or {}).get("code", ""),
            "promo_date_to": (promo or {}).get("dateTo", ""),
            "base_price_per_day_pln": base,
            "promo_price_per_day_pln": promo_price if promo_price is not None else "",
        }
        rows.extend(tier_rows(base_row, discounts, base, promo_price))

    if not rows:
        return [], "no_matching_diets"
    return rows, None


def parse_calories_arg(values: list[str] | None) -> set[int] | None:
    if not values:
        return None
    if len(values) == 1 and values[0].lower() in ("any", "all"):
        return None
    return {int(v) for v in values}


def parse_diet_tags_arg(values: list[str] | None) -> set[str] | None:
    if not values:
        return None
    upper = [v.upper() for v in values]
    if upper == ["ALL"]:
        return None
    return set(upper)


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Scrape diet prices from dietly.pl. Pass --city (and any "
        "filter flags) for scripted mode, or run without arguments for the "
        "interactive wizard. By default no filters are applied — every flag "
        "narrows the result set."
    )
    p.add_argument("--city", help="City slug, e.g. wroclaw, warszawa, krakow")
    p.add_argument(
        "--calories",
        nargs="+",
        metavar="KCAL",
        help=(
            "Allowed kcal values. Omit for no filter. "
            "Common values: " + ", ".join(map(str, COMMON_CALORIES))
        ),
    )
    p.add_argument(
        "--meals",
        type=int,
        metavar="N",
        help="Required meals per day, e.g. --meals 5. Omit for no filter.",
    )
    p.add_argument(
        "--diet-tags",
        nargs="+",
        metavar="TAG",
        help=(
            "Allowed diet tags. Omit for no filter. "
            "Known: " + ", ".join(KNOWN_DIET_TAGS)
        ),
    )
    p.add_argument(
        "--include-menu-config",
        action="store_true",
        help="Include 'Wybór menu' diets (excluded by default — usually pricier).",
    )
    p.add_argument("--output", "-o", help="Output CSV path")
    p.add_argument("--workers", type=int, default=5, help="Parallel workers (default: 5)")
    p.add_argument(
        "--delay",
        type=float,
        default=0.2,
        help="Per-worker delay between API calls in seconds (default: 0.2)",
    )
    p.add_argument("--page-size", type=int, default=15, help="Listing page size (default: 15)")
    return p


def args_to_config(args: argparse.Namespace) -> RunConfig:
    cals = parse_calories_arg(args.calories)
    tags = parse_diet_tags_arg(args.diet_tags)
    meals = None if args.meals is None or args.meals <= 0 else args.meals

    output = args.output or f"dietly_{args.city}_{date.today().isoformat()}.csv"
    return RunConfig(
        city=args.city,
        output=output,
        workers=args.workers,
        delay=args.delay,
        page_size=args.page_size,
        filters=FilterConfig(
            diet_tags=tags,
            calories=cals,
            meals_count=meals,
            include_menu_config=args.include_menu_config,
        ),
    )


def interactive_config(defaults: argparse.Namespace) -> RunConfig:
    """Step-by-step prompts. CLI args (other than filters) act as pre-fill."""

    city_choices = [questionary.Choice(name, value=slug) for name, slug in POPULAR_CITIES]
    city_choices.append(questionary.Choice("Other city (type manually)", value="__other__"))
    city = questionary.select(
        "City:",
        choices=city_choices,
        use_arrow_keys=True,
    ).ask()
    if city is None:
        sys.exit(0)
    if city == "__other__":
        city = questionary.text("City slug (e.g. wroclaw, krakow):").ask()
        if not city:
            sys.exit(0)

    selected_cals = questionary.checkbox(
        "Calorie values (space=toggle, enter=confirm; empty=no filter):",
        choices=[questionary.Choice(str(c), value=c) for c in COMMON_CALORIES],
    ).ask()
    if selected_cals is None:
        sys.exit(0)
    calories = set(selected_cals) if selected_cals else None

    meals_str = questionary.text(
        "Meals per day (empty=no filter):",
    ).ask()
    if meals_str is None:
        sys.exit(0)
    meals = int(meals_str.strip()) if meals_str.strip() else None
    if meals is not None and meals <= 0:
        meals = None

    selected_tags = questionary.checkbox(
        "Diet types (empty=no filter):",
        choices=[questionary.Choice(t, value=t) for t in KNOWN_DIET_TAGS],
    ).ask()
    if selected_tags is None:
        sys.exit(0)
    diet_tags = set(selected_tags) if selected_tags else None

    include_menu_config = questionary.confirm(
        "Include 'Wybór menu' diets (usually pricier than ready-made)?",
        default=False,
    ).ask()
    if include_menu_config is None:
        sys.exit(0)

    output = defaults.output or f"dietly_{city}_{date.today().isoformat()}.csv"
    return RunConfig(
        city=city,
        output=output,
        workers=defaults.workers,
        delay=defaults.delay,
        page_size=defaults.page_size,
        filters=FilterConfig(
            diet_tags=diet_tags,
            calories=calories,
            meals_count=meals,
            include_menu_config=include_menu_config,
        ),
    )


def describe_filters(f: FilterConfig) -> str:
    parts = []
    parts.append(
        f"diet_tags={'/'.join(sorted(f.diet_tags))}" if f.diet_tags else "diet_tags=any"
    )
    parts.append(
        f"calories={'/'.join(map(str, sorted(f.calories)))}"
        if f.calories
        else "calories=any"
    )
    parts.append(f"meals={f.meals_count}" if f.meals_count is not None else "meals=any")
    parts.append(
        "menu_config=included" if f.include_menu_config else "menu_config=excluded"
    )
    return ", ".join(parts)


def run(cfg: RunConfig) -> int:
    session = make_session()

    print(f"Fetching listings for city={cfg.city}...", file=sys.stderr)
    print(f"Filters: {describe_filters(cfg.filters)}", file=sys.stderr)
    caterings, city_id = fetch_all_listings(session, cfg.city, cfg.page_size)
    if city_id is None:
        print(f"ERROR: city '{cfg.city}' not found by dietly API", file=sys.stderr)
        return 2

    print(
        f"Found {len(caterings)} caterings for {cfg.city} (cityId={city_id}).",
        file=sys.stderr,
    )

    rows_all: list[dict[str, Any]] = []
    skip_reasons: dict[str, int] = {}
    processed = 0

    with ThreadPoolExecutor(max_workers=cfg.workers) as ex:
        futures = {
            ex.submit(
                process_catering, session, cfg.city, city_id, c, cfg.delay, cfg.filters
            ): c
            for c in caterings
        }
        for fut in tqdm(as_completed(futures), total=len(futures), desc="caterings"):
            processed += 1
            try:
                rows, reason = fut.result()
            except Exception as e:
                skip_reasons[f"exception:{type(e).__name__}"] = (
                    skip_reasons.get(f"exception:{type(e).__name__}", 0) + 1
                )
                continue
            if reason:
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            rows_all.extend(rows)

    with open(cfg.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows_all)

    n_with_rows = len({r["catering_fullname"] for r in rows_all})
    n_skipped = len(caterings) - n_with_rows
    print("", file=sys.stderr)
    print(f"Done. Wrote {len(rows_all)} rows to {cfg.output}", file=sys.stderr)
    print(
        f"Caterings processed: {processed}, with rows: {n_with_rows}, skipped: {n_skipped}",
        file=sys.stderr,
    )
    if skip_reasons:
        print("Skip reasons:", file=sys.stderr)
        for k, v in sorted(skip_reasons.items(), key=lambda x: -x[1]):
            print(f"  {k}: {v}", file=sys.stderr)
    return 0


def main() -> int:
    parser = build_argparser()
    args = parser.parse_args()

    if args.city:
        cfg = args_to_config(args)
    else:
        cfg = interactive_config(args)

    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
