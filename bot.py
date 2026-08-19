import os
import threading
import logging
import math
import requests
import psycopg2
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from timezonefinder import TimezoneFinder
from datetime import time, datetime
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw
from io import BytesIO
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, PicklePersistence

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
load_dotenv()
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_KEY = os.environ.get("API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")
HELP_TEXT = (
    " <b>Bot Help & Commands</b>\n\n"
    "• <b>Look up traffic:</b> <code>/traffic &lt;city&gt;</code>\n"
    "  E.g.: <code>/traffic Warsaw</code>\n\n"
    "• <b>Daily reports:</b> <code>/setdaily &lt;city&gt; &lt;time&gt;</code>\n"
    "  E.g.: <code>/setdaily Freiburg 07:00</code>\n\n"
    "• <b>View subscription:</b> <code>/viewdaily</code>\n"
    "• <b>Cancel subscription:</b> <code>/canceldaily</code>\n\n"
    "• <b>Live traffic monitoring:</b> <code>/setreminder &lt;city&gt; &lt;min_delay&gt; [interval_min] [show_repeating]</code>\n"
    "  E.g.: <code>/setreminder London 5 10 false</code>\n\n"
    "• <b>Stop live monitoring:</b> <code>/stopreminder</code>\n"
    "• <b>Show help menu:</b> <code>/info</code>"
)
tf = TimezoneFinder()
USER_REMINDERS = {}

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")


def start_health_check():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

def init_db():
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    chat_id BIGINT PRIMARY KEY,
                    city TEXT NOT NULL,
                    time_str TEXT NOT NULL,
                    tz_str TEXT NOT NULL
                );
            """)

async def check_traffic_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    chat_id = job.chat_id
    data = USER_REMINDERS.get(chat_id)

    if not data:
        return

    city = data["city"]
    min_delay = data["min_delay"]
    show_repeating = data.get("show_repeating", True)
    seen_jams = data.get("seen_jams", set())

    try:
        geo_url = f"https://api.tomtom.com/search/2/geocode/{city}.json"
        geo_res = requests.get(geo_url, params={"key": API_KEY, "limit": 1}).json()
        results = geo_res.get("results")
        if not results:
            return

        pos = results[0]["position"]
        lat, lon = pos["lat"], pos["lon"]

        delta = 0.12
        bbox = f"{lon - delta},{lat - delta},{lon + delta},{lat + delta}"

        incident_url = "https://api.tomtom.com/traffic/services/5/incidentDetails"
        params = {
            "key": API_KEY,
            "bbox": bbox,
            "fields": "{incidents{properties{id,iconCategory,delay,events{description},from,to,length}}}",
            "language": "en-GB",
            "timeValidityFilter": "present"
        }
        inc_res = requests.get(incident_url, params=params).json()
        incidents = inc_res.get("incidents", [])

        current_jam_ids = set()
        new_alerts = []

        for inc in incidents:
            props = inc.get("properties", {})
            icon_cat = props.get("iconCategory")
            events = props.get("events") or []
            desc = events[0].get("description", "").lower() if events else ""

            is_jam = (icon_cat == 6) or ("jam" in desc) or ("traffic" in desc) or ("queue" in desc)
            if not is_jam:
                continue

            delay_min = round((props.get("delay") or 0) / 60)
            if delay_min < min_delay:
                continue

            jam_id = props.get("id") or f"{props.get('from')}-{props.get('to')}"
            current_jam_ids.add(jam_id)

            is_new = jam_id not in seen_jams
            if show_repeating or is_new:
                from_loc = props.get("from", "Unknown")
                to_loc = props.get("to", "Unknown")
                length_km = round((props.get("length") or 0) / 1000, 1)

                status_tag = "Ongoing" if not is_new else "New"

                new_alerts.append(
                    f"🚨 <b>Traffic Alert ({status_tag}) - {city.title()}</b>\n"
                    f"• <b>Location:</b> between <i>{from_loc}</i> and <i>{to_loc}</i>\n"
                    f"• <b>Delay:</b> +{delay_min} min\n"
                    f"• <b>Queue Length:</b> {length_km} km"
                )

        USER_REMINDERS[chat_id]["seen_jams"] = current_jam_ids

        for alert in new_alerts:
            await context.bot.send_message(chat_id=chat_id, text=alert, parse_mode="HTML")

    except Exception as e:
        logging.error(f"Error in traffic reminder job: {e}")


async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_message.chat_id

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: <code>/setreminder &lt;city&gt; &lt;min_delay&gt; [interval_min] [show_repeating]</code>\n\n"
            "Examples:\n"
            "• Default (every 5 min, repeats on): <code>/setreminder London 5</code>\n"
            "• Check every 10 min: <code>/setreminder London 5 10</code>\n"
            "• New jams only (no repeats every 5 min): <code>/setreminder London 5 5 false</code>",
            parse_mode="HTML"
        )
        return

    city = context.args[0]

    try:
        min_delay = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Please provide a valid number for delay minutes.")
        return

    interval_min = 5
    show_repeating = True

    if len(context.args) >= 3:
        try:
            interval_min = int(context.args[2])
            if interval_min < 1:
                interval_min = 1
        except ValueError:
            await update.message.reply_text("Interval must be a valid number of minutes.")
            return

    if len(context.args) >= 4:
        show_repeating = context.args[3].lower() in ("true", "1", "yes", "y", "t")

    interval_sec = interval_min * 60

    current_jobs = context.job_queue.get_jobs_by_name(str(chat_id))
    for job in current_jobs:
        job.schedule_removal()

    USER_REMINDERS[chat_id] = {
        "city": city,
        "min_delay": min_delay,
        "interval": interval_sec,
        "show_repeating": show_repeating,
        "seen_jams": set()
    }

    context.job_queue.run_repeating(
        check_traffic_job,
        interval=interval_sec,
        first=5,
        chat_id=chat_id,
        name=str(chat_id)
    )

    repeat_status = "Enabled" if show_repeating else "Disabled (New jams only)"
    await update.message.reply_text(
        f"🔔 Reminder set!\n\n"
        f"• <b>City:</b> {city.title()}\n"
        f"• <b>Threshold:</b> {min_delay}+ min delay\n"
        f"• <b>Check Frequency:</b> Every {interval_min} min\n"
        f"• <b>Repeated Alerts:</b> {repeat_status}",
        parse_mode="HTML"
    )


async def stop_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_message.chat_id
    jobs = context.job_queue.get_jobs_by_name(str(chat_id))

    if not jobs:
        await update.message.reply_text("You don't have any active traffic reminders.")
        return

    for job in jobs:
        job.schedule_removal()

    USER_REMINDERS.pop(chat_id, None)
    await update.message.reply_text("🔕 Traffic reminder stopped.")

def save_subscription(chat_id: int, city: str, time_str: str, tz_str: str):
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO subscriptions (chat_id, city, time_str, tz_str)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (chat_id) 
                DO UPDATE SET city = EXCLUDED.city, time_str = EXCLUDED.time_str, tz_str = EXCLUDED.tz_str;
            """, (chat_id, city, time_str, tz_str))

def remove_subscription(chat_id: int):
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM subscriptions WHERE chat_id = %s;", (chat_id,))

def restore_subscriptions(app):
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT chat_id, city, time_str, tz_str FROM subscriptions;")
            rows = cur.fetchall()
            for chat_id, city, time_str, tz_str in rows:
                hour, minute = map(int, time_str.split(":"))
                tz = ZoneInfo(tz_str)
                target_time = time(hour=hour, minute=minute, tzinfo=tz)
                job_name = f"daily_traffic_{chat_id}"

                app.job_queue.run_daily(
                    send_daily_traffic_job,
                    time=target_time,
                    chat_id=chat_id,
                    name=job_name,
                    data={
                        "chat_id": chat_id,
                        "city": city,
                        "time_str": time_str,
                        "tz_str": tz_str
                    }
                )

def latlon_to_tile(lat, lon, zoom):
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (
        1.0
        - math.asinh(math.tan(lat_rad)) / math.pi
    ) / 2.0 * n
    return x, y

def tile_to_latlon(x, y, zoom):
    n = 2 ** zoom
    lon = x / n * 360.0 - 180.0
    lat_rad = math.atan(
        math.sinh(math.pi * (1 - 2 * y / n))
    )
    lat = math.degrees(lat_rad)
    return lat, lon

def generate_traffic_map(
    lat,
    lon,
    zoom=12,
    width=1024,
    height=600,
    jam_markers=None
):
    try:
        if jam_markers is None:
            jam_markers = []

        center_x, center_y = latlon_to_tile(lat, lon, zoom)

        tiles_x = math.ceil(width / 256) + 2
        tiles_y = math.ceil(height / 256) + 2

        start_x = math.floor(center_x - tiles_x / 2)
        start_y = math.floor(center_y - tiles_y / 2)

        max_tile = 2 ** zoom

        canvas_width = tiles_x * 256
        canvas_height = tiles_y * 256

        base_map = Image.new(
            "RGBA",
            (canvas_width, canvas_height),
            (255, 255, 255, 255)
        )

        traffic_overlay = Image.new(
            "RGBA",
            (canvas_width, canvas_height),
            (0, 0, 0, 0)
        )

        for tx in range(tiles_x):
            for ty in range(tiles_y):

                tile_x = start_x + tx
                tile_y = start_y + ty

                if tile_y < 0 or tile_y >= max_tile:
                    continue

                request_x = tile_x % max_tile

                map_url = (
                    f"https://api.tomtom.com/"
                    f"map/1/tile/basic/main/"
                    f"{zoom}/{request_x}/{tile_y}.png"
                )

                map_response = requests.get(
                    map_url,
                    params={
                        "key": API_KEY,
                        "tileSize": 256
                    },
                    timeout=10
                )

                if map_response.status_code != 200:
                    logging.warning(
                        f"Map tile failed: "
                        f"{zoom}/{request_x}/{tile_y}"
                    )
                    continue

                map_tile = Image.open(
                    BytesIO(map_response.content)
                ).convert("RGBA")

                px = tx * 256
                py = ty * 256

                base_map.alpha_composite(
                    map_tile,
                    (px, py)
                )

                traffic_url = (
                    f"https://api.tomtom.com/"
                    f"traffic/map/4/tile/flow/"
                    f"relative0/"
                    f"{zoom}/{request_x}/{tile_y}.png"
                )

                traffic_response = requests.get(
                    traffic_url,
                    params={
                        "key": API_KEY,
                        "tileSize": 256
                    },
                    timeout=10
                )

                if traffic_response.status_code != 200:
                    logging.warning(
                        f"Traffic tile failed: "
                        f"{zoom}/{request_x}/{tile_y}"
                    )
                    continue

                traffic_tile = Image.open(
                    BytesIO(traffic_response.content)
                ).convert("RGBA")

                traffic_overlay.alpha_composite(
                    traffic_tile,
                    (px, py)
                )

        base_map.alpha_composite(traffic_overlay)

        center_pixel_x = (
            center_x - start_x
        ) * 256

        center_pixel_y = (
            center_y - start_y
        ) * 256

        left = int(center_pixel_x - width / 2)
        top = int(center_pixel_y - height / 2)

        result = base_map.crop(
            (
                left,
                top,
                left + width,
                top + height
            )
        )

        draw = ImageDraw.Draw(result)

        for marker_lon, marker_lat, number, is_heavy in jam_markers:

            marker_x, marker_y = latlon_to_tile(
                marker_lat,
                marker_lon,
                zoom
            )

            pixel_x = (
                marker_x - start_x
            ) * 256 - left

            pixel_y = (
                marker_y - start_y
            ) * 256 - top

            pixel_x = int(pixel_x)
            pixel_y = int(pixel_y)

            if not (
                0 <= pixel_x < width
                and 0 <= pixel_y < height
            ):
                continue
            radius = 16
            draw.ellipse(
                (
                    pixel_x - radius - 2,
                    pixel_y - radius - 2,
                    pixel_x + radius + 2,
                    pixel_y + radius + 2
                ),
                fill=(255, 255, 255, 255)
            )

            pin_fill = (220, 40, 40, 255) if is_heavy else (245, 140, 0, 255)

            draw.ellipse(
                (
                    pixel_x - radius,
                    pixel_y - radius,
                    pixel_x + radius,
                    pixel_y + radius
                ),
                fill=pin_fill
            )

            text = str(number)

            bbox = draw.textbbox(
                (0, 0),
                text
            )

            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]

            draw.text(
                (
                    pixel_x - text_width / 2,
                    pixel_y - text_height / 2 - 2
                ),
                text,
                fill=(255, 255, 255, 255)
            )

        output = BytesIO()
        result.save(output, format="PNG")
        output.seek(0)
        output.name = "traffic_map.png"

        return output

    except Exception as e:
        logging.error(
            f"Failed to generate traffic map: {e}"
        )
        return None

def fetch_city_jams_and_map(city_name: str):
    try:
        geo_url = f"https://api.tomtom.com/search/2/geocode/{city_name}.json"
        geo_res = requests.get(geo_url, params={"key": API_KEY, "limit": 1}).json()

        results = geo_res.get("results")
        if not results:
            return [f"Couldn't find coordinates for '{city_name}'."], None

        pos = results[0]["position"]
        lat, lon = pos["lat"], pos["lon"]

        delta = 0.12
        bbox = f"{lon - delta},{lat - delta},{lon + delta},{lat + delta}"

        incident_url = "https://api.tomtom.com/traffic/services/5/incidentDetails"
        params = {
            "key": API_KEY,
            "bbox": bbox,
            "fields": "{incidents{properties{iconCategory,delay,events{description},from,to,length},geometry{type,coordinates}}}",
            "language": "en-GB",
            "timeValidityFilter": "present"
        }
        inc_res = requests.get(incident_url, params=params).json()
        incidents = inc_res.get("incidents", [])

        raw_jams = []

        for inc in incidents:
            props = inc.get("properties", {})
            icon_cat = props.get("iconCategory")
            events = props.get("events") or []
            desc = events[0].get("description", "").lower() if events else ""

            is_jam = (icon_cat == 6) or ("jam" in desc) or ("traffic" in desc) or ("queue" in desc)
            if not is_jam:
                continue

            delay_sec = props.get("delay") or 0
            from_loc = props.get("from") or ""
            to_loc = props.get("to") or ""
            length_meters = props.get("length") or 0

            geom = inc.get("geometry", {})
            coords = geom.get("coordinates", [])

            mid_point = None
            if coords and geom.get("type") == "LineString":
                mid_point = coords[len(coords) // 2]

            delay_min = round(delay_sec / 60)
            length_km = round(length_meters / 1000, 1)

            loc_str = ""
            if from_loc and to_loc:
                loc_str = f"between <i>{from_loc}</i> and <i>{to_loc}</i>"
            elif from_loc:
                loc_str = f"at <i>{from_loc}</i>"
            else:
                loc_str = "Unknown location"

            raw_jams.append({
                "delay_min": delay_min,
                "length_km": length_km,
                "loc_str": loc_str,
                "mid_point": mid_point
            })

        if not raw_jams:
            return [f"🟢 <b>Traffic Report for {city_name.title()}</b>\n\nNo traffic jams reported right now!"], None

        raw_jams.sort(key=lambda x: x["delay_min"], reverse=True)

        jam_lines = []
        jam_markers = []

        for number, jam in enumerate(raw_jams, 1):
            is_heavy = jam["delay_min"] >= 10
            emoji = "🔴" if is_heavy else "🟠"

            if jam["mid_point"]:
                jam_markers.append(
                    (
                        jam["mid_point"][0],
                        jam["mid_point"][1],
                        number,
                        is_heavy
                    )
                )

            stats = []
            if jam["delay_min"] > 0:
                stats.append(f"+{jam['delay_min']} min delay")
            if jam["length_km"] > 0:
                stats.append(f"{jam['length_km']} km queue")
            stats_str = f" <b>({', '.join(stats)})</b>" if stats else ""

            jam_lines.append(
                f"{emoji} <b>Traffic Jam #{number}</b> "
                f"{jam['loc_str']}{stats_str}"
            )

        photo_bytes = generate_traffic_map(
            lat,
            lon,
            zoom=12,
            width=1024,
            height=600,
            jam_markers=jam_markers
        )

        messages = []
        header = f"🚨 <b>Traffic Jams in {city_name.title()} ({len(jam_lines)} total)</b>\n\n"
        current_msg = header

        for line in jam_lines:
            if len(current_msg) + len(line) + 1 > 3500:
                messages.append(current_msg)
                current_msg = line + "\n"
            else:
                current_msg += line + "\n"
        if current_msg:
            messages.append(current_msg)

        return messages, photo_bytes

    except Exception as e:
        logging.error(f"Error fetching traffic: {e}")
        return [f"Error retrieving traffic info for {city_name}."], None

def get_city_coordinates(city_name: str):
    try:
        geo_url = f"https://api.tomtom.com/search/2/geocode/{city_name}.json"
        geo_res = requests.get(geo_url, params={"key": API_KEY, "limit": 1}).json()

        results = geo_res.get("results")
        if not results:
            return None, None

        pos = results[0]["position"]
        return pos["lat"], pos["lon"]
    except Exception as e:
        logging.error(f"Geocoding error: {e}")
        return None, None

async def send_daily_traffic_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data["chat_id"]
    city = job.data["city"]

    reports, photo_data = fetch_city_jams_and_map(city)

    if photo_data:
        try:
            caption_text = f"⏰ <b>Daily Traffic Report for {city.title()}</b>"
            await context.bot.send_photo(chat_id=chat_id, photo=photo_data, caption=caption_text, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Failed to send daily scheduled photo: {e}")

    for report in reports:
        await context.bot.send_message(chat_id=chat_id, text=report, parse_mode="HTML")

async def setdaily_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = context.args

    if len(args) < 2:
        await update.message.reply_text(
            "⚠️ Usage: `/setdaily <City> <HH:MM>`\n"
            "Example: `/setdaily London 07:30`",
            parse_mode="Markdown"
        )
        return

    time_str = args[-1]
    city = " ".join(args[:-1])

    try:
        hour, minute = map(int, time_str.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (ValueError, TypeError):
        await update.message.reply_text("⚠️ Invalid time format. Please use 24-hour HH:MM format (e.g. `07:30`).")
        return

    lat, lon = get_city_coordinates(city)
    if lat is None or lon is None:
        await update.message.reply_text(f"❌ Couldn't find coordinates for '{city}'. Please check the spelling.")
        return

    tz_str = tf.timezone_at(lat=lat, lng=lon)
    if not tz_str:
        tz_str = "UTC"

    tz = ZoneInfo(tz_str)
    target_time = time(hour=hour, minute=minute, tzinfo=tz)

    job_name = f"daily_traffic_{chat_id}"

    current_jobs = context.job_queue.get_jobs_by_name(job_name)
    for j in current_jobs:
        j.schedule_removal()
    save_subscription(chat_id, city, time_str, tz_str)
    context.job_queue.run_daily(
        send_daily_traffic_job,
        time=target_time,
        chat_id=chat_id,
        name=job_name,
        data={
            "chat_id": chat_id,
            "city": city,
            "time_str": time_str,
            "tz_str": tz_str
        }
    )

    await update.message.reply_text(
        f"✅ <b>Daily traffic reminder set!</b>\n\n"
        f"📍 <b>City:</b> {city.title()}\n"
        f"⏰ <b>Local Time:</b> {time_str}\n"
        f"🌍 <b>Detected Timezone:</b> {tz_str}",
        parse_mode="HTML"
    )

async def viewdaily_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT city, time_str, tz_str FROM subscriptions WHERE chat_id = %s;", (chat_id,))
            row = cur.fetchone()

    if not row:
        await update.message.reply_text(
            "You don't have any active daily traffic subscriptions.\nSet one with `/setdaily <city> <HH:MM>`",
            parse_mode="Markdown"
        )
        return

    city, time_str, tz_str = row

    await update.message.reply_text(
        f"📅 <b>Your Current Daily Subscription:</b>\n\n"
        f"📍 <b>City:</b> {city.title()}\n"
        f"⏰ <b>Time:</b> {time_str} ({tz_str})",
        parse_mode="HTML"
    )

async def canceldaily_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    job_name = f"daily_traffic_{chat_id}"
    remove_subscription(chat_id)
    current_jobs = context.job_queue.get_jobs_by_name(job_name)
    if not current_jobs:
        await update.message.reply_text("You don't have an active daily subscription to cancel.")
        return

    for job in current_jobs:
        job.schedule_removal()

    await update.message.reply_text("❌ Your daily traffic subscription has been canceled.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML")


async def info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML")

async def traffic_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please provide a city name! Example: `/traffic Warsaw`")
        return

    city = " ".join(context.args)
    status_msg = await update.message.reply_text(f"Fetching traffic data and generating map for {city}...")

    reports, photo_data = fetch_city_jams_and_map(city)

    if photo_data:
        try:
            caption_text = f"Live Traffic Jam Map for {city.title()}"
            await update.message.reply_photo(photo=photo_data, caption=caption_text)
        except Exception as e:
            logging.error(f"Failed to send photo: {e}")
            await update.message.reply_text("Failed to generate/send the traffic map image.")

    for report in reports:
        await update.message.reply_text(report, parse_mode="HTML")

    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg.message_id)


def main():
    threading.Thread(target=start_health_check, daemon=True).start()
    init_db()
    persistence = PicklePersistence(filepath="bot_persistence.pickle")
    app = ApplicationBuilder().token(BOT_TOKEN).persistence(persistence).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("traffic", traffic_command))
    app.add_handler(CommandHandler("setdaily", setdaily_command))
    app.add_handler(CommandHandler("viewdaily", viewdaily_command))
    app.add_handler(CommandHandler("canceldaily", canceldaily_command))
    app.add_handler(CommandHandler("setreminder", set_reminder))
    app.add_handler(CommandHandler("stopreminder", stop_reminder))
    app.add_handler(CommandHandler("info", info))
    restore_subscriptions(app)
    print("Bot`s running.")
    app.run_polling()


if __name__ == "__main__":
    main()
