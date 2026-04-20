import json
import requests
from supabase import create_client
import os

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

with open("draft.json", "r", encoding="utf-8") as f:
    data = json.load(f)

profiles = data["profiles"]
bucket = "rookies-headshots"

for player in profiles:
    player_id = player["person"]["id"]
    name = player["person"]["displayName"]
    url = player.get("headshot")

    if not url:
        continue

    url = url.replace("{formatInstructions}", "w_300")

    try:
        img = requests.get(url, timeout=10).content
        file_path = f"{player_id}.jpg"

        # Upload
        supabase.storage.from_(bucket).upload(
            file_path,
            img,
            {"content-type": "image/jpeg"}
        )

        public_url = supabase.storage.from_(bucket).get_public_url(file_path)

        # DB Update (anpassen!)
        supabase.table("players").update({
            "headshot_url": public_url
        }).eq("player_id", player_id).execute()

        print("OK:", name)

    except Exception as e:
        print("ERROR:", name, e)
