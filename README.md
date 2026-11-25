# sf2-multiplayer-mod

A multiplayer mod for Shadow Fight 2. Two players fight each other on one PC, or each on their own
device over the network. It runs on **PC, Android and iPhone**.

The game you play is the **web version of Shadow Fight 2**, patched by this mod. It plays in a browser
(PC, iPhone) or in the mod's Android app. The game's own fights against the AI are still there as
usual. The mod's match menu adds free fights: any character against any other, played by a person or
the AI.

**This repository contains no game files.** The mod is built from your own copy of the web version,
which comes with the Microsoft Store app on Windows. This project is not affiliated with or endorsed by
Nekki or Famobi. All game content belongs to its owners.

## What you need

- To build: a Windows PC with Shadow Fight 2 installed from the Microsoft Store (it contains the web
  version), Python 3.10 or newer and `pip install zstandard pillow`
- To play: a browser on any PC or iPhone, or the Android app
- Java, only for building the Android app

## 1. Copy the game folder

The game lives in a protected Windows folder:

```
C:\Program Files\WindowsApps\Famobi.ShadowFight2_<version>_x64__5y4rhc0xaat4m\www
```

Copy the whole `www` folder somewhere you can write, e.g. `C:\sf2\www`. Windows may ask for
administrator rights to open `WindowsApps`. The builder never changes `www`.

## 2. Build

```
python builder.py C:\sf2\www -o C:\sf2\out
```

`out` is the patched game. Build again after a game update or a new version of this tool.

## 3. Play

```
python server.py C:\sf2\out
```

Open http://localhost:8080/ in Chrome or Edge. The menu has:

- **Single player**: you against the AI
- **Two players, this PC**: both on one keyboard
- **Two players, network**: each on their own device
- **Normal game**: the game as it is, with its own save

In a free fight you pick the fighters, the place, the rounds and the game's own fight rules (darkness,
earthquakes, ring-out ...). Every perk, enchantment and rule has a description with its numbers.

### Your own fighter

Tick **My own fighter** on your card, type a name and pick a picture. The first time you choose your
gear and perk tree in the dojo; its fight button starts the match and saves them. From then on the
fighter is ready without the dojo; tick **Change gear and perk tree in the dojo first** to change it.
Your name and picture show on the VS screen.

### Sharing the game with other devices

A PC or an Android phone with the app can share the game. Other devices on the same network (phones,
tablets, iPhones, other PCs) then open it in their browser; they need nothing installed. No internet
is needed: a phone's hotspot is enough.

- **From a PC:** `python server.py C:\sf2\out --share`, then the others open
  `http://<this PC's IP>:8080/`. Windows may ask to let Python through the firewall.
- **From the Android app:** **Two players, network > Share the game**. The app shows the address the
  others open. Keep the app open while they play.

### Two players on two devices

One player chooses **Two players, network > Create match**. The other enters the host's address
and joins. The host can be a PC, the Android app, or a device that opened a shared game (its match
then runs through the sharing device). Only the inputs travel; both devices run the same simulation.

Each key press is used a few frames after it is pressed (the input delay, shown at the bottom of the
screen), so it reaches the other device in time. The delay is set from the ping when the match starts
and grows by a frame if the other device keeps waiting. A fixed value can be set with
`lan_input_delay` in the match JSON.

## Android app (optional)

```
python builder.py C:\sf2\out --apk
```

The app's files are in `android/`: `sf2_shell.apk` (the app without the game), `apksigner.jar`
(Google's signing tool) and the app's source in `android/project`. The first run creates a signing key
`android/sf2.keystore`. Keep it, and never upload it: an app signed with another key does not install
over the old one.

## Command reference

`python builder.py --help` and `python server.py --help` list every option, including `match.json`
files and `server.py --list`.

## License

The code in this repository is under the MIT License (see `LICENSE`). It does not cover the game.
