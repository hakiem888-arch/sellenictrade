"""Optional continuously running scanner for a computer/VPS that stays on."""
import argparse
import os
from pathlib import Path
import time
import tomllib
from alerts import Telegram, message
from market import BinanceFutures, MarketError


def credentials():
    path = Path(".streamlit/secrets.toml")
    secrets = tomllib.loads(path.read_text()) if path.exists() else {}
    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_TOKEN") or secrets.get("TELEGRAM_BOT_TOKEN") or secrets.get("TG_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TG_CHAT") or secrets.get("TELEGRAM_CHAT_ID") or secrets.get("TG_CHAT", "")
    return token, chat


def main():
    parser = argparse.ArgumentParser(description="Momentum Scanner — futures + Telegram")
    parser.add_argument("--mode", choices=["SCALPING", "INTRADAY", "SWING"], default="INTRADAY")
    parser.add_argument("--pairs", type=int, default=30)
    parser.add_argument("--symbols", default="", help="BTCUSDT,ETHUSDT,SOLUSDT; kosong = peringkat volume")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--min-volume", type=float, default=10_000_000)
    parser.add_argument("--telegram", action="store_true", help="Aktifkan pengiriman alert kandidat teknis")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.pairs <= 200 or args.interval < 30 or args.min_volume < 0:
        parser.error("pairs 1–200, interval minimal 30 detik, min-volume nonnegatif")
    tg = Telegram(*credentials())
    if args.telegram and not tg.configured:
        parser.error("Isi TELEGRAM_BOT_TOKEN dan TELEGRAM_CHAT_ID di environment atau secrets.toml")
    client = BinanceFutures()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    while True:
        try:
            report = client.scan(args.mode, args.pairs, args.min_volume, symbols)
            candidates = [r for r in report["results"] if r.candidate]
            print(f"{args.mode}: {len(report['results'])} pair · {len(candidates)} kandidat · {len(report['errors'])} kendala", flush=True)
            for r in candidates:
                # Revalidate at dispatch: a broad scan may take longer than 30 seconds.
                if client.now() - r.checked_at > 15_000:
                    r = client.inspect(r.symbol, r.mode, report["contracts"][r.symbol], fresh=True)
                if not r.candidate:
                    continue
                if args.telegram:
                    _, note = tg.send(message(r), r.key)
                    print(r.symbol, note, flush=True)
                else:
                    print(r.symbol, "kandidat", r.direction, flush=True)
            for err in report["errors"]:
                print(err["Pair"], err["Kendala"], flush=True)
        except (MarketError, ValueError, KeyError, TypeError) as exc:
            print(f"WAIT — {exc}", flush=True)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Scanner dihentikan.")
