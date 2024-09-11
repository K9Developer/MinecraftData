import glob
import io
import json
import math
import os
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any

import requests
from bs4 import BeautifulSoup
from PIL import Image
from io import BytesIO

ICON_SIZE = 64
wiki_atlas = None
spawn_eggs_soup = None
session = requests.Session()


def get_spawn_egg_data(item):
    global wiki_atlas, spawn_eggs_soup

    if spawn_eggs_soup is None:
        url = "https://minecraft.fandom.com/wiki/Spawn_Egg"
        response = session.get(url)
        spawn_eggs_soup = BeautifulSoup(response.text, "html.parser")

    header = spawn_eggs_soup.find("span", class_="mw-headline", id="Data_values")
    h2 = header.find_parent("h2")
    table = h2.find_next_sibling("table")
    for row in table.find_all("tr")[1:]:
        tds = row.find_all("td")
        if tds[1].text == item["mc_id"]:
            img_span = tds[0].find("span")
            bg_img_url = img_span["style"].split("url(")[1].split(")")[0]
            bg_pos = img_span["style"].split("background-position:")[1].split(";")[0]
            offset_x = -int(bg_pos.split(" ")[0].replace("px", ""))
            offset_y = -int(bg_pos.split(" ")[1].replace("px", ""))
            if wiki_atlas is None:
                response = session.get(bg_img_url)
                wiki_atlas = Image.open(BytesIO(response.content)).convert("RGBA")
            img = wiki_atlas.crop((offset_x, offset_y, offset_x + 16, offset_y + 16))
            img = img.resize((ICON_SIZE, ICON_SIZE), Image.NEAREST)
            return img

    return None


def get_data(row):
    tds = row.find_all("td")
    image_url = tds[0].find("a")
    if image_url.get("href"):
        image_url = image_url["href"]
    else:
        image_url = image_url.find("img")
        image_url = image_url.get("data-src") or image_url["src"]
    mc_id = tds[1].text
    name = tds[2].text.strip().replace("\\n", "")
    return {"image_url": image_url, "mc_id": mc_id, "name": name}


def get_blocks():
    print("🧱 Getting blocks...")
    url = "https://minecraft.fandom.com/api.php?action=parse&format=json&prop=text%7Cmodules%7Cjsconfigvars&title=Java_Edition_data_values&text=%7B%7B%3AJava%20Edition%20data%20values%2FBlocks%7D%7D"
    response = session.get(url)
    data = response.json()
    soup = BeautifulSoup(data["parse"]["text"]["*"], "html.parser")
    table = soup.find("table", attrs={"data-description": "Block IDs"})
    return [dict(get_data(row), type="block") for row in table.find_all("tr")[1:]]


def get_items():
    print("🔧 Getting items...")
    url = "https://minecraft.fandom.com/api.php?action=parse&format=json&prop=text%7Cmodules%7Cjsconfigvars&title=Java_Edition_data_values&text=%7B%7B%3AJava%20Edition%20data%20values%2FItems%7D%7D"
    response = session.get(url)
    data = response.json()
    soup = BeautifulSoup(data["parse"]["text"]["*"], "html.parser")
    table = soup.find("table", attrs={"data-description": "Item IDs"})
    items = []
    for row in table.find_all("tr")[1:]:
        item_data = get_item_data(row)
        items.append(item_data)
    return items


def get_item_data(row):
    td1, td2 = row.find_all("td")[:2]
    name = td1.text.strip()
    mc_id = td2.text.strip()
    return {"name": name, "mc_id": mc_id, "type": "item"}


def process_item(item: Dict[str, Any], z: zipfile.ZipFile) -> Dict[str, Any]:
    try:
        with z.open(f"assets/minecraft/textures/item/{item['mc_id']}.png") as f:
            img = Image.open(io.BytesIO(f.read())).convert("RGBA")
            aspect_ratio = img.width / img.height
            img = img.resize((ICON_SIZE, int(ICON_SIZE / aspect_ratio)), Image.NEAREST)
    except KeyError:
        if item['mc_id'].endswith("_spawn_egg"):
            img = get_spawn_egg_data(item)
        else:
            img = None

    return {"item": item, "img": img}


def process_block(block: Dict[str, Any]) -> Dict[str, Any]:
    response = session.get(block["image_url"])
    img = Image.open(BytesIO(response.content)).convert("RGBA")
    img.thumbnail((ICON_SIZE, ICON_SIZE), Image.NEAREST)

    return {"block": block, "img": img}


def main():
    print("🚀 Starting Minecraft Atlas Generator")

    os.chdir(os.path.dirname(os.path.realpath(__file__)))
    java_files = glob.glob("src/**/CreativeModeTabs.java", recursive=True)
    ordered_items = []
    with open(java_files[0], "r") as f:
        data = f.read()
        ordered_items = [i.lower() for i in re.findall(r"\s*output.accept\(Items\.(.*)\);", data)]

    block_data = get_blocks()
    item_data = get_items()

    cells = len(block_data) + len(item_data)
    width = int(math.sqrt(cells)) * ICON_SIZE
    height = math.ceil(cells / (width // ICON_SIZE)) * ICON_SIZE
    atlas_image = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    print("🖼️ Creating atlas image...")
    current_offset_x = 0
    current_offset_y = 0
    metadata = []

    file_path = glob.glob("versions/*/client.jar")[0]
    with zipfile.ZipFile(file_path, "r") as z:
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(process_item, item, z) for item in item_data]
            for i, future in enumerate(as_completed(futures)):
                result = future.result()
                item, img = result["item"], result["img"]
                print(f"📦 Processing item {i + 1}/{len(item_data)}: {item['mc_id']}")
                if img:
                    atlas_image.paste(img, (current_offset_x, current_offset_y))
                    metadata.append({
                        "name": item["name"],
                        "id": item["mc_id"],
                        "type": item["type"],
                        "offsetX": current_offset_x,
                        "offsetY": current_offset_y
                    })
                    current_offset_x += ICON_SIZE
                    if current_offset_x >= width:
                        current_offset_x = 0
                        current_offset_y += ICON_SIZE

    print("🧱 Processing blocks...")
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(process_block, block) for block in block_data]
        for i, future in enumerate(as_completed(futures)):
            result = future.result()
            block, img = result["block"], result["img"]
            print(f"🧱 Processing block {i + 1}/{len(block_data)}: {block['mc_id']}")
            atlas_image.paste(img, (current_offset_x, current_offset_y))
            metadata.append({
                "name": block["name"],
                "id": block["mc_id"],
                "type": block["type"],
                "offsetX": current_offset_x,
                "offsetY": current_offset_y
            })
            current_offset_x += ICON_SIZE
            if current_offset_x >= width:
                current_offset_x = 0
                current_offset_y += ICON_SIZE

    print("💾 Saving atlas image...")
    atlas_image.save("atlas.png")

    # Sorting metadata
    print("🔍 Sorting metadata...")
    new_metadata = []
    added_ids = set()
    for item in ordered_items:
        for data in metadata:
            if data["id"] == item:
                new_metadata.append(data)
                added_ids.add(item)
                break
    # Adding missing items
    for data in metadata:
        if data["id"] not in added_ids:
            new_metadata.append(data)

    print("📝 Writing metadata...")
    with open("atlas_metadata.json", "w") as f:
        json.dump(metadata, f, indent=4)

    print("✅ Minecraft Atlas Generator completed successfully!")


if __name__ == "__main__":
    main()