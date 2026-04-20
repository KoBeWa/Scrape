import json
import requests
from supabase import create_client
import os

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

with open("draft.json", "r", encoding="utf-8") as f:
    data = json.load(f)

bucket = "rookies-headshots"

for player in data["profiles"]:
    name = player["person"]["displayName"]
    position = player.get("position")
    college = player["person"]["collegeNames"][0] if player["person"].get("collegeNames") else None

    url = player.get("headshot")
    if not url:
        continue

    url = url.replace("{formatInstructions}", "w_300")

    try:
        # Bild holen
        img = requests.get(url, timeout=10).content

        file_path = f"{player['person']['id']}.jpg"

        # Upload Storage
        supabase.storage.from_(bucket).upload(
            file_path,
            img,
            {"content-type": "image/jpeg"}
        )

        public_url = supabase.storage.from_(bucket).get_public_url(file_path)

        # 🔥 WICHTIG: in deine Tabelle schreiben
        supabase.table("mock_draft_prospects").upsert({
            "player_name": name,
            "position": position,
            "college": college,
            "headshot_url": public_url
        }).execute()

        print("OK:", name)

    except Exception as e:
        print("ERROR:", name, e)
