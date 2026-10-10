# Copyright (c) 2026, Renaud Allard <renaud@allard.it>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""Dump raw `event_map` entries for one or more cameras over a time window.

Diagnostic tool for LEGACY_EVENT_BITMASK.md — see its "Contributing" section for
the full procedure. Prints every `event_map` bucket whose `flag` isn't
just 0/1 (i.e. every bucket carrying real event information), so you can
line them up against an `eventlog` cross-reference.

Usage:
    python3 scripts/dump_event_map.py CAMERA[,CAMERA...] --date YYYY-MM-DD \\
        --from HH:MM:SS --to HH:MM:SS [--profile NAME] [--all]

CAMERA is matched as a case-sensitive substring against each configured
camera's display name; pass a comma-separated list to cover several
cameras in one call. --profile defaults to the app's configured default
connection profile. --all also prints the "nothing happened" (flag 0/1)
buckets, which are omitted by default.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from surveillance.api.auth import login
from surveillance.api.client import SurveillanceAPI
from surveillance.config import load_config
from surveillance.credentials import get_credentials
from surveillance.services.camera import list_cameras


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cameras", help="Comma-separated camera name substring(s)")
    parser.add_argument("--date", help="YYYY-MM-DD, defaults to today")
    parser.add_argument("--from", dest="from_time", required=True, help="HH:MM:SS")
    parser.add_argument("--to", dest="to_time", required=True, help="HH:MM:SS")
    parser.add_argument("--profile", help="Connection profile name, defaults to the app's default")
    parser.add_argument(
        "--all", action="store_true", help="Also print flag=0/1 buckets (omitted by default)"
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    config = load_config()
    profile_name = args.profile or config.default_profile
    profile = config.profiles.get(profile_name)
    if profile is None:
        print(f"No connection profile named {profile_name!r} in config")
        return

    creds = get_credentials(profile.name)
    if not creds or not creds[0]:
        print(f"No stored credentials found for profile {profile.name!r}")
        return
    username, password = creds

    api = SurveillanceAPI(profile)
    await api.discover_apis()
    await login(api, username, password, device_id=profile.device_id)

    day = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else datetime.now().date()
    sh, sm, ss = (int(x) for x in args.from_time.split(":"))
    eh, em, es = (int(x) for x in args.to_time.split(":"))
    start = datetime.combine(day, datetime.min.time()) + timedelta(hours=sh, minutes=sm, seconds=ss)
    end = datetime.combine(day, datetime.min.time()) + timedelta(hours=eh, minutes=em, seconds=es)
    from_time = int((start - timedelta(minutes=1)).timestamp())
    to_time = int((end + timedelta(minutes=1)).timestamp())

    cameras = await list_cameras(api)
    substrs = args.cameras.split(",")
    targets = []
    for substr in substrs:
        for camera in cameras:
            if substr in camera.name:
                targets.append(camera)
                break
    if not targets:
        print("No matching cameras found. Configured cameras:")
        for camera in cameras:
            print(f"  id={camera.id} name={camera.name!r}")
        await api.close()
        return

    for camera in targets:
        print(f"# {camera.name} — camera_id={camera.id}")

    content = [{"dsId": 0, "archId": 0, "mountId": 0, "camList": [c.id for c in targets]}]
    data = await api.request(
        api="SYNO.SurveillanceStation.RecordingPicker",
        method="EnumInterval",
        version=1,
        extra_params={
            "from": str(from_time),
            "to": str(to_time),
            "content": json.dumps(content),
            "recording": "true",
            "blStartTimeAsc": "true",
            "blGetMetaMap": "true",
            "interval": "5",
            "blExcludeC2": "true",
        },
        timeout=60.0,
    )

    print(
        f"\nQuery window: {from_time} .. {to_time} "
        f"({datetime.fromtimestamp(from_time)} .. {datetime.fromtimestamp(to_time)})\n"
    )

    for entry in data.get("cameras", []):
        for cam in entry:
            camera_id = cam.get("camera_id", 0)
            cam_name = next((c.name for c in targets if c.id == camera_id), "?")
            t = from_time
            for value, flag, reserved in cam.get("event_map", []):
                duration = value * 5
                run_start, run_stop = t, t + duration
                t = run_stop
                if not args.all and flag in (0, 1) and reserved == 0:
                    continue
                start_str = datetime.fromtimestamp(run_start).strftime("%H:%M:%S")
                stop_str = datetime.fromtimestamp(run_stop).strftime("%H:%M:%S")
                print(
                    f"cam={camera_id} ({cam_name}) {start_str}-{stop_str} "
                    f"start_epoch={run_start} flag={flag} reserved={reserved}"
                )

    await api.close()


if __name__ == "__main__":
    asyncio.run(main())
