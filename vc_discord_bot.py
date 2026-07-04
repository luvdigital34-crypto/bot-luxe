import json
import os
import asyncio
import statistics
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone

import requests
import discord
from discord import app_commands
from discord.ext import tasks

# ===================== CONFIGURATION =====================

BOT_TOKEN             = os.environ.get("DISCORD_BOT_TOKEN", "COLLE_TON_TOKEN_DISCORD")
CHECK_INTERVAL_SEC    = 300        # Intervalle entre deux checks (secondes)
MAX_ITEM_AGE_MINUTES  = 30         # Ignorer les articles plus vieux que X minutes
MIN_PROFIT_EUR        = 50         # Marge minimale pour envoyer une alerte
REQUIRE_RESALE        = True       # Ignorer si estimation impossible
MIN_COMPARABLES       = 5          # Nombre minimum d'articles comparables
DATA_FILE             = Path(__file__).parent / "watches.json"

API_URL  = "https://search.vestiairecollective.com/v1/product/search"
BASE_URL = "https://www.vestiairecollective.com"
IMG_BASE = "https://images.vestiairecollective.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Content-Type": "application/json",
    "Origin": "https://fr.vestiairecollective.com",
    "Referer": "https://fr.vestiairecollective.com/",
}

# ===================== STOCKAGE =====================

def load_watches() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return {}

def save_watches(w: dict):
    DATA_FILE.write_text(json.dumps(w, indent=2))

def parse_query_param(url: str) -> str:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    return params.get("q", [""])[0].replace("+", " ")

# ===================== HELPERS PARSING =====================

def build_product_url(item: dict) -> str:
    link = item.get("link", "")
    if link.startswith("/"):
        url = BASE_URL + link
        print(f"[url] {link} → {url}")
        return url
    if link.startswith("http"):
        print(f"[url] déjà complète: {link}")
        return link
    item_id = item.get("id", "")
    print(f"[url] aucun link pour id={item_id}, skip")
    return ""

def build_image_url(item: dict) -> str:
    pictures = item.get("pictures", [])
    if not pictures:
        print(f"[img] aucune image pour id={item.get('id')}")
        return ""
    pic = pictures[0]
    if isinstance(pic, str):
        url = pic if pic.startswith("http") else IMG_BASE + pic
        print(f"[img] {pic} → {url}")
        return url
    if isinstance(pic, dict):
        raw = pic.get("url") or pic.get("src") or ""
        url = raw if raw.startswith("http") else IMG_BASE + raw
        print(f"[img] dict → {url}")
        return url
    return ""

def parse_price(item: dict) -> float:
    price_data = item.get("price", {})
    if isinstance(price_data, dict):
        cents = price_data.get("cents", 0)
        return float(cents) / 100 if cents else 0.0
    return float(price_data) if price_data else 0.0

def parse_created_at(item: dict) -> datetime | None:
    ts = item.get("createdAt")
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except:
        return None

def is_recent_item(item: dict) -> tuple[bool, str]:
    dt = parse_created_at(item)
    if dt is None:
        print(f"[age] id={item.get('id')} — pas de createdAt, on accepte")
        return True, "inconnu"
    now     = datetime.now(tz=timezone.utc)
    age_min = (now - dt).total_seconds() / 60
    print(f"[age] id={item.get('id')} — âge: {age_min:.1f} min (max: {MAX_ITEM_AGE_MINUTES})")
    if age_min > MAX_ITEM_AGE_MINUTES:
        return False, f"{age_min:.0f} min"
    return True, f"{age_min:.0f} min"

def format_age(item: dict) -> str:
    dt = parse_created_at(item)
    if dt is None:
        return "date inconnue"
    now     = datetime.now(tz=timezone.utc)
    age_min = int((now - dt).total_seconds() / 60)
    if age_min < 60:
        return f"il y a {age_min} min"
    age_h = age_min // 60
    return f"il y a {age_h}h"

def parse_brand(item: dict) -> str:
    brand = item.get("brand", {})
    return brand.get("name", "") if isinstance(brand, dict) else str(brand)

def parse_size(item: dict) -> str:
    size = item.get("size", "")
    return size.get("name", "") if isinstance(size, dict) else str(size)

def parse_condition(item: dict) -> str:
    cond = item.get("condition", "")
    return cond.get("name", "") if isinstance(cond, dict) else str(cond)

# ===================== ESTIMATION REVENTE =====================

def percentile(data: list, p: float) -> float:
    data = sorted(data)
    k    = (len(data) - 1) * p / 100
    f, c = int(k), min(int(k) + 1, len(data) - 1)
    return data[f] + (data[c] - data[f]) * (k - f)

def fetch_similar_items(item: dict) -> list[float]:
    brand = parse_brand(item)
    name  = item.get("name", "")
    query = f"{brand} {name}".strip()
    print(f"[resale] recherche comparables: '{query}'")

    payload = {
        "pagination": {"offset": 0, "limit": 60},
        "facets":     {},
        "fields":     ["price", "sold", "id"],
        "filters":    {},
        "locale":     {"country": "FR", "currency": "EUR", "language": "fr"},
        "options":    {"innerFeedContext": "genericPLP"},
        "q":          query,
        "recentlyViewedProductIds": [],
        "sortBy":     "relevance",
    }
    try:
        resp  = requests.post(API_URL, headers=HEADERS, json=payload, timeout=15)
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except Exception as e:
        print(f"[resale] erreur API: {e}")
        return []

    current_id = str(item.get("id", ""))
    prices     = []
    for i in items:
        if i.get("sold", False):
            continue
        if str(i.get("id", "")) == current_id:
            continue
        p = parse_price(i)
        if p > 0:
            prices.append(p)

    print(f"[resale] {len(prices)} prix trouvés: {sorted(prices)[:10]}")
    return prices

def estimate_resale_price(item: dict) -> dict:
    prices = fetch_similar_items(item)

    if len(prices) < MIN_COMPARABLES:
        print(f"[resale] insuffisant: {len(prices)} < {MIN_COMPARABLES}")
        return {"ok": False, "reason": f"seulement {len(prices)} comparables"}

    prices.sort()
    cut     = max(1, int(len(prices) * 0.10))
    trimmed = prices[cut:-cut] if len(prices) > cut * 2 else prices

    if len(trimmed) < 2:
        return {"ok": False, "reason": "pas assez après écrêtage"}

    low    = round(percentile(trimmed, 25))
    median = round(statistics.median(trimmed))
    high   = round(percentile(trimmed, 75))

    seller_price = parse_price(item)
    profit       = round(median - seller_price)
    roi          = round((profit / seller_price) * 100) if seller_price > 0 else 0

    print(f"[resale] bas={low}€ médiane={median}€ haut={high}€ profit={profit}€ ROI={roi}%")

    return {
        "ok":     True,
        "low":    low,
        "median": median,
        "high":   high,
        "profit": profit,
        "roi":    roi,
        "count":  len(trimmed),
    }

# ===================== FILTRE BONNE AFFAIRE =====================

def is_good_deal(product: dict) -> tuple[bool, str]:
    resale = product.get("resale", {})

    if not resale.get("ok"):
        reason = resale.get("reason", "estimation indisponible")
        if REQUIRE_RESALE:
            return False, f"estimation échouée: {reason}"
        return True, "ok (estimation ignorée)"

    profit = resale.get("profit", 0)
    if profit < MIN_PROFIT_EUR:
        return False, f"marge {profit}€ < minimum {MIN_PROFIT_EUR}€"

    return True, "ok"

# ===================== EMBED DISCORD =====================

def create_discord_embed(product: dict) -> discord.Embed:
    resale = product.get("resale", {})
    profit = resale.get("profit", 0)
    roi    = resale.get("roi", 0)

    emoji  = "🔥" if profit >= 100 else "✅"
    embed  = discord.Embed(
        title=f"{emoji} {product['title']}",
        url=product["url"],
        color=0xB8860B,
    )

    # Prix vendeur
    embed.add_field(
        name="💰 Prix vendeur",
        value=f"**{product['price_raw']:.0f} €**",
        inline=True,
    )

    # Estimation revente
    if resale.get("ok"):
        embed.add_field(
            name="📈 Estimation revente",
            value=f"Fourchette : {resale['low']} € — {resale['high']} €\nPrix médian : **{resale['median']} €**",
            inline=True,
        )
        profit_str = f"+{profit} €" if profit >= 0 else f"{profit} €"
        roi_str    = f"+{roi} %" if roi >= 0 else f"{roi} %"
        embed.add_field(
            name="✅ Profit estimé",
            value=f"**{profit_str}**",
            inline=True,
        )
        embed.add_field(
            name="📊 ROI estimé",
            value=f"**{roi_str}**",
            inline=True,
        )
    else:
        embed.add_field(
            name="📈 Estimation revente",
            value="Données insuffisantes",
            inline=True,
        )

    # Détails produit
    details_lines = []
    if product.get("brand"):
        details_lines.append(f"Marque : {product['brand']}")
    if product.get("size"):
        details_lines.append(f"Taille : {product['size']}")
    if product.get("condition"):
        details_lines.append(f"État : {product['condition']}")
    if product.get("age"):
        details_lines.append(f"Publié : {product['age']}")

    if details_lines:
        embed.add_field(
            name="📦 Détails",
            value="\n".join(details_lines),
            inline=False,
        )

    # Image
    if product.get("image") and product["image"].startswith("http"):
        embed.set_image(url=product["image"])

    count = resale.get("count", 0)
    embed.set_footer(
        text=f"Basé sur {count} articles similaires • Vestiaire Collective bot-luxe"
        if count else "Vestiaire Collective • bot-luxe"
    )
    return embed

# ===================== FETCH LISTINGS =====================

def fetch_listings(search_url: str) -> list:
    query   = parse_query_param(search_url) or "sac louis vuitton"
    payload = {
        "pagination": {"offset": 0, "limit": 60},
        "facets":     {},
        "fields":     ["name", "brand", "price", "link", "pictures", "size", "condition", "createdAt", "sold"],
        "filters":    {},
        "locale":     {"country": "FR", "currency": "EUR", "language": "fr"},
        "options":    {"innerFeedContext": "genericPLP"},
        "q":          query,
        "recentlyViewedProductIds": [],
        "sortBy":     "relevance",
    }
    resp  = requests.post(API_URL, headers=HEADERS, json=payload, timeout=20)
    resp.raise_for_status()
    items = resp.json().get("items", [])

    listings = []
    for item in items:
        item_id = str(item.get("id") or "")
        if not item_id or item.get("sold", False):
            continue

        brand     = parse_brand(item)
        name      = item.get("name") or "Article"
        title     = f"{brand} — {name}" if brand and brand.lower() not in name.lower() else name
        price_raw = parse_price(item)
        url       = build_product_url(item)
        image     = build_image_url(item)

        listings.append({
            "id":        item_id,
            "url":       url,
            "title":     title[:256],
            "name":      name,
            "brand":     brand,
            "price_raw": price_raw,
            "image":     image,
            "size":      parse_size(item),
            "condition": parse_condition(item),
            "age":       format_age(item),
            "_item":     item,  # on garde l'item brut pour les filtres
        })

    return listings

# ===================== BOT DISCORD =====================

intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)

@tree.command(name="watch_add", description="Ajoute une recherche Vestiaire Collective à surveiller")
@app_commands.describe(url="URL de recherche VC", label="Nom court (ex: lv-speedy, dior-sac)")
async def watch_add(interaction: discord.Interaction, url: str, label: str):
    watches = load_watches()
    if label in watches:
        await interaction.response.send_message(f"Veille `{label}` existe déjà.", ephemeral=True)
        return
    watches[label] = {"url": url, "channel_id": interaction.channel_id, "seen": []}
    save_watches(watches)
    await interaction.response.send_message(
        f"✅ Veille **{label}** ajoutée ! Alertes toutes les ~{CHECK_INTERVAL_SEC // 60} min.\n"
        f"Filtre : marge min {MIN_PROFIT_EUR}€ | articles récents ({MAX_ITEM_AGE_MINUTES} min max)"
    )

@tree.command(name="watch_list", description="Liste les veilles actives")
async def watch_list(interaction: discord.Interaction):
    watches = load_watches()
    if not watches:
        await interaction.response.send_message("Aucune veille active.", ephemeral=True)
        return
    lines = [f"• **{l}** → <#{w['channel_id']}>" for l, w in watches.items()]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@tree.command(name="watch_remove", description="Supprime une veille")
@app_commands.describe(label="Nom de la veille à supprimer")
async def watch_remove(interaction: discord.Interaction, label: str):
    watches = load_watches()
    if label not in watches:
        await interaction.response.send_message(f"Pas de veille `{label}`.", ephemeral=True)
        return
    del watches[label]
    save_watches(watches)
    await interaction.response.send_message(f"🗑️ Veille **{label}** supprimée.")

@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Bot connecté : {client.user}")
    if not check_watches.is_running():
        check_watches.start()

@tasks.loop(seconds=CHECK_INTERVAL_SEC)
async def check_watches():
    watches = load_watches()
    if not watches:
        return

    for label, watch in list(watches.items()):
        print(f"\n[~] Check '{label}'...")
        try:
            listings = fetch_listings(watch["url"])
        except Exception as e:
            print(f"[!] Erreur API '{label}': {e}")
            continue

        seen      = set(watch.get("seen", []))
        first_run = len(seen) == 0
        new_items = [l for l in listings if l["id"] not in seen]
        channel   = client.get_channel(watch["channel_id"])

        if first_run:
            print(f"[i] '{label}': premier passage, {len(listings)} annonces mémorisées.")
        else:
            for listing in new_items:
                item = listing["_item"]

                # Filtre 1 : URL valide
                if not listing["url"] or listing["url"] == BASE_URL:
                    print(f"[skip] id={listing['id']} — URL invalide")
                    continue

                # Filtre 2 : article récent
                recent, age_str = is_recent_item(item)
                if not recent:
                    print(f"[skip] id={listing['id']} — trop vieux ({age_str})")
                    continue

                # Filtre 3 : estimation revente
                resale = estimate_resale_price(item)
                listing["resale"] = resale

                # Filtre 4 : bonne affaire
                good, reason = is_good_deal(listing)
                if not good:
                    print(f"[skip] id={listing['id']} — {reason}")
                    continue

                print(f"[+] ALERTE: {listing['title']} — {listing['price_raw']}€ — marge: {resale.get('profit', '?')}€")

                if channel:
                    try:
                        await channel.send(
                            content=f"🆕 Nouvelle affaire détectée — veille **{label}**",
                            embed=create_discord_embed(listing)
                        )
                    except Exception as e:
                        print(f"[!] Erreur envoi Discord: {e}")
                await asyncio.sleep(2)

        watch["seen"] = list({l["id"] for l in listings} | seen)
        watches[label] = watch

    save_watches(watches)

if __name__ == "__main__":
    if BOT_TOKEN == "COLLE_TON_TOKEN_DISCORD":
        print("ERREUR : configure la variable DISCORD_BOT_TOKEN")
    else:
        client.run(BOT_TOKEN)
