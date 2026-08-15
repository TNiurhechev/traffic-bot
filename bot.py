import os
import threading
import logging
import math
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from timezonefinder import TimezoneFinder
from datetime import time
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
tf = TimezoneFinder()


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")


def start_health_check():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

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

        for marker_lon, marker_lat, number in jam_markers:

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

            draw.ellipse(
                (
                    pixel_x - radius,
                    pixel_y - radius,
                    pixel_x + radius,
                    pixel_y + radius
                ),
                fill=(220, 40, 40, 255)
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

        jam_lines = []
        jam_markers = []

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

            if coords and geom.get("type") == "LineString":
                mid_point = coords[len(coords) // 2]
                jam_markers.append(
                    (
                        mid_point[0],
                        mid_point[1],
                        len(jam_markers) + 1
                    )
                )

            delay_min = round(delay_sec / 60)
            length_km = round(length_meters / 1000, 1)

            loc_str = ""
            if from_loc and to_loc:
                loc_str = f"between <i>{from_loc}</i> and <i>{to_loc}</i>"
            elif from_loc:
                loc_str = f"at <i>{from_loc}</i>"
            else:
                loc_str = "Unknown location"

            stats = []
            if delay_min > 0:
                stats.append(f"+{delay_min} min delay")
            if length_km > 0:
                stats.append(f"{length_km} km queue")
            stats_str = f" <b>({', '.join(stats)})</b>" if stats else ""

            jam_number = len(jam_lines) + 1

            jam_lines.append(
                f"🔴 <b>Traffic Jam #{jam_number}</b> "
                f"{loc_str}{stats_str}"
            )

        if not jam_lines:
            return [f"🟢 <b>Traffic Report for {city_name.title()}</b>\n\nNo traffic jams reported right now!"], None

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
    job_name = f"daily_traffic_{chat_id}"

    current_jobs = context.job_queue.get_jobs_by_name(job_name)
    if not current_jobs:
        await update.message.reply_text("You don't have any active daily traffic subscriptions.\nSet one with `/setdaily <city> <HH:MM>`", parse_mode="Markdown")
        return

    job_data = current_jobs[0].data
    city = job_data["city"]
    time_str = job_data["time_str"]
    tz_str = job_data["tz_str"]

    await update.message.reply_text(
        f"📅 <b>Your Current Daily Subscription:</b>\n\n"
        f"📍 <b>City:</b> {city.title()}\n"
        f"⏰ <b>Time:</b> {time_str} ({tz_str})",
        parse_mode="HTML"
    )


async def canceldaily_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    job_name = f"daily_traffic_{chat_id}"

    current_jobs = context.job_queue.get_jobs_by_name(job_name)
    if not current_jobs:
        await update.message.reply_text("You don't have an active daily subscription to cancel.")
        return

    for job in current_jobs:
        job.schedule_removal()

    await update.message.reply_text("❌ Your daily traffic subscription has been canceled.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot started.\n"
                                    "To look up traffic jams, type /traffic {city name}.\n"
                                    "E.g.: /traffic Warsaw\n"
                                    "To set daily notifications, type /setdaily {city name} {time}\n"
                                    "E.g.: /setdaily London 07:30\n"
                                    "To view current subscription, use /viewdaily\n"
                                    "Cancel your subscription at any time with /canceldaily", parse_mode="HTML")


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
    persistence = PicklePersistence(filepath="bot_persistence.pickle")
    app = ApplicationBuilder().token(BOT_TOKEN).persistence(persistence).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("traffic", traffic_command))
    app.add_handler(CommandHandler("setdaily", setdaily_command))
    app.add_handler(CommandHandler("viewdaily", viewdaily_command))
    app.add_handler(CommandHandler("canceldaily", canceldaily_command))
    print("Bot`s running.")
    app.run_polling()
if __name__ == "__main__":
    main()