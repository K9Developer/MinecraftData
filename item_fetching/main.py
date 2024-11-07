import glob
import io
import json
import math
import os
import re
import zipfile
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
import requests
from PIL import Image
from io import BytesIO

# Constants
ICON_SIZE = 64
MAX_WORKERS = 10  # Slightly reduced concurrency
TIMEOUT = 10

# Configure session for connection pooling and human-like headers
session = requests.Session()
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.93 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Connection": "keep-alive"
}
session.headers.update(headers)

# Add a random delay function to mimic human behavior
def random_delay(min_delay=0.5, max_delay=2.0):
    time.sleep(random.uniform(min_delay, max_delay))

def get_item_name(item, item_keys):
    if f"item.minecraft.{item}" in item_keys:
        return item_keys[f"item.minecraft.{item}"], "item"
    elif f"block.minecraft.{item}" in item_keys:
        return item_keys[f"block.minecraft.{item}"], "block"
    return None, None

def fetch_versions():
    print("📥 Fetching Minecraft version data...")
    versions_url = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
    random_delay()  # Random delay before request
    versions = session.get(versions_url, timeout=TIMEOUT).json()
    latest_version = versions["versions"][0]
    random_delay()  # Another random delay before the next request
    version = session.get(latest_version["url"], timeout=TIMEOUT).json()
    return version["downloads"]["client"]["url"]

def load_language_file(client_url):
    print("📖 Loading language files...")
    random_delay()  # Delay before loading the language file
    client = session.get(client_url, timeout=TIMEOUT).content
    with zipfile.ZipFile(io.BytesIO(client)) as jar:
        en_us_json = json.loads(jar.read("assets/minecraft/lang/en_us.json"))
    return {k: v for k, v in en_us_json.items() 
            if k.startswith(("item.minecraft.", "block.minecraft."))}

def try_fetch_icon(url):
    random_delay()  # Random delay before request
    response = session.get(url)
    if response.status_code == 200:
        return url, response.content
    else:
        print(url, response.status_code, response.content)
    return None, None

def fetch_item_parallel(args):
    item, item_keys, total_items, current_item = args
    print(f"📦 Processing item {current_item}/{total_items}: {item}")
    
    item_data = {
        "name": item,
        "mc_id": item,
        "icon": None,
        "type": "item",
        "content": None
    }
    
    # Clean up item name
    clean_item = item.replace("infested_", "").replace("_smithing_template", "").replace("waxed_", "")
    if clean_item == "cut_standstone_slab": 
        clean_item = "cut_sandstone_slab"
    
    # Get item name and type
    name, type_ = get_item_name(clean_item, item_keys)
    if name:
        item_data["name"] = name
        item_data["type"] = type_
    
    # Generate possible URLs
    item_id = '_'.join(a.title() for a in clean_item.split('_'))
    urls = [
        f"https://minecraft.wiki/images/Invicon_{item_id}.png",
        f"https://minecraft.wiki/images/Invicon_{item_id}.gif",
        f"https://minecraft.wiki/images/ItemSprite_{clean_item.replace('_', '-')}.png"
    ]
    
    if name:
        formatted_name = name.replace(" ", "_")
        urls.extend([
            f"https://minecraft.wiki/images/Invicon_{formatted_name}.png",
            f"https://minecraft.wiki/images/Invicon_{formatted_name}.gif"
        ])
    
    for url in urls:
        item_data["icon"], item_data["content"] = try_fetch_icon(url)
        if item_data["content"]:
            break
    
    if not item_data["content"]:
        print(f"❌ No icon found for {item}")
    
    return item_data

def process_image(content):
    """Process image content to exact ICON_SIZE"""
    if not content:
        return Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    
    # Open and convert to RGBA
    img = Image.open(BytesIO(content)).convert("RGBA")
    
    # Create a new image with exact ICON_SIZE dimensions
    new_img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    
    # Resize image maintaining aspect ratio
    aspect = img.width / img.height
    if aspect > 1:
        new_width = ICON_SIZE
        new_height = int(ICON_SIZE / aspect)
    else:
        new_height = ICON_SIZE
        new_width = int(ICON_SIZE * aspect)
    
    img = img.resize((new_width, new_height), Image.Resampling.NEAREST)
    
    # Center the image
    x_offset = (ICON_SIZE - new_width) // 2
    y_offset = (ICON_SIZE - new_height) // 2
    
    # Paste onto new image
    new_img.paste(img, (x_offset, y_offset), img)
    
    return new_img

def main():
    print("🚀 Starting Minecraft Atlas Generator")
    
    # Load item categories
    os.chdir(os.path.dirname(os.path.realpath(__file__)))
    os.system("python decompilermc.py -mcv snap -s client -d cfr")
    java_files = glob.glob("src/**/CreativeModeTabs.java", recursive=True)
    
    print("📑 Reading creative mode tabs...")
    item_cats = {}
    curr_cat = None
    with open(java_files[0], "r") as f:
        for line in f:
            cat_match = re.findall(r"^\s*Registry\.register\(registry, (\w*), .*", line)
            if cat_match:
                curr_cat = cat_match[0]
                item_cats[curr_cat] = []
            elif curr_cat in item_cats:
                items = re.findall(r"\s*output.accept\(Items\.(.*)\);", line)
                item_cats[curr_cat].extend(item.lower() for item in items)

    # Get unique ordered items
    ordered_items = list(dict.fromkeys(item for items in item_cats.values() for item in items))
    total_items = len(ordered_items)
    print(f"📋 Found {total_items} unique items")
    
    # Fetch version data and language file
    client_url = fetch_versions()
    item_keys = load_language_file(client_url)
    
    print("🖼️ Fetching item data in parallel...")
    item_data_list = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(
                fetch_item_parallel, 
                (item, item_keys, total_items, idx + 1)
            ) 
            for idx, item in enumerate(ordered_items)
        ]
        for future in as_completed(futures):
            item_data_list.append(future.result())
            random_delay(1, 3)  # Random delay after processing each item
    
    # Create atlas
    cells = len(ordered_items)
    width = int(math.sqrt(cells)) * ICON_SIZE
    height = math.ceil(cells / (width // ICON_SIZE)) * ICON_SIZE
    atlas_image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    
    # Process images and create metadata
    print("💾 Creating atlas and metadata...")
    metadata = []
    x, y = 0, 0
    for idx, item_data in enumerate(item_data_list, 1):
        print(f"🎨 Adding item to atlas ({idx}/{total_items}): {item_data['name']}")
        img = process_image(item_data["content"])
        atlas_image.paste(img, (x, y))
        metadata.append({
            "name": item_data["name"],
            "id": item_data["mc_id"],
            "type": item_data["type"],
            "offsetX": x,
            "offsetY": y
        })
        x += ICON_SIZE
        if x >= width:
            x = 0
            y += ICON_SIZE
    
    print("💾 Saving atlas image...")
    atlas_image.save("../items/atlas.png", optimize=True)
    
    print("📝 Writing metadata...")
    metadata = sorted(metadata, key=lambda x: ordered_items.index(x["id"]))
    with open("../items/atlas_metadata.json", "w") as f:
        json.dump(metadata, f, indent=4)
    
    print(f"✅ Atlas generation complete! Created atlas with {total_items} items")
    print(f"   Atlas dimensions: {width}x{height} pixels")
    print(f"   Output files: atlas.png and atlas_metadata.json")

if __name__ == "__main__":
    main()
