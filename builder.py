#!/usr/bin/env python3
"""
builder.py - Shadow Fight 2 mod: builds the patched game (out) from the game folder (www), and the Android app.

    pip install zstandard pillow
    python builder.py <www> [-o out]             out is <www>_out by default; then: python server.py <out>
    python builder.py <out> --apk [file.apk]     the Android app from a built out (needs Java)
    python builder.py <www> -o out --apk         both at once
    python builder.py <www> --dump-xml ZONE_7/C3_Survival/1     prints a game fight's <Fight> element
Android content (an android_* folder next to www, made by android.py) is added to a copy of www first.
--android FOLDER picks another folder, --android none builds without it.

www is never changed. out is www plus:
  sf2.js          the game with our patches (JS_PATCHES, JS_HEADER)
  xml2.dat        tutorial quests emptied, every item owned, max level
  sf2data.js      game data for the patches and the menu; match.json is checked against it
  sf2menu/        the match menu (MENU_JS) and its item pictures
  sf2list.json    what server.py --list prints
  index.html      the menu, then the game; match.js says "no match" (server.py sends its own)
The match is chosen in the browser, so one build serves every match. The APK is android/sf2_shell.apk
(WebView and network server) plus out, signed with android/sf2.keystore (made on the first --apk; keep it).

match.json holds only the keys you change; the rest comes from DEFAULTS. Two kinds:
  game fight    {"fight": "ZONE_7/C3_Survival/1"}: a fight as the game has it (its opponents, groups and
                rules; "eclipse": true for Eclipse mode). You are the player.
  custom match  both sides, the place and the rules set in match.json (it replaces zone ZONE_T)
The menu checks it (__sf2Check); errors show in the menu and in server.py's window.
With one human player the game starts in the dojo (every item) and its fight button starts the match.
Otherwise it goes straight into the match and starts a new one when it ends.

Controls
  same PC, two humans : left  = W A S D,  F punch  G kick  R ranged  T magic  Q super
                        right = arrows,   , punch  . kick  / ranged  ; magic  ' super  (or numpad 1 2 4 5 0)
  same PC, one human  : both key sets control that side
  network             : either key set (or the game's K L O P), or the touch controls on a phone.
                        Both devices run the same simulation; only inputs travel. Status bar at the bottom.
  phone               : an "S" (Super) button shows while your fighter has a Super perk
  gamepad             : Super on the right shoulder button
"""
import argparse
import copy
import functools
import glob
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from xml.sax.saxutils import quoteattr

try:
    import zstandard
except ImportError:
    sys.exit("pip install zstandard")

DEFAULTS = {
    # a fight of the game ("ZONE/BATTLE/FIGHT", see --list battles) with its own opponents, rules and place;
    # you are the player. Only the settings marked "game fight" below apply to it
    "fight": None,
    "eclipse": False,                  # game fight + custom: the game's Eclipse mode (Eclipse="0|1" rules / difficulty)
    # game fight and custom match vs AI: one of DIFFICULTIES on the game's own scale (None = as it is).
    # Game fight: your level is set so that the best gear of that level rates there.
    # Custom match: the AI side gets stronger or weaker until it rates there
    "difficulty": None,
    "language": None,                  # game fight + custom: game text language: None = the device's; or one of LANGUAGES
    "lan_input_delay": "auto",         # game fight + custom, network: frames (1/60 s) between a key press and its effect; "auto" = from ping
    # ---- custom match (no "fight")
    "location": "bamboo_grove",       # --list locations
    "music": "fight1_samurai_spirit",  # --list music
    "rounds": 3,
    "round_time": 99,                  # seconds
    "battle_type": "CHALLENGE",        # CHALLENGE, BOSSES, TOURNAMENT, DUMMY (no timer) ...
    "rules": [],                       # raw XML, e.g. "<Attributes DamageFactor=\"2000\" ApplyTo=\"Player\"/>"
    "fight_attrs": {},                 # other <Fight> attributes, e.g. {"HealthRecovery": 0} (no health back between rounds)
    "left": {                          # game fight: the player (only "human", "tactic", "items" apply)
        "human": True,                 # False = AI with "tactic"
        "template": "Default",         # --list templates; null = no template
        "name": None,                  # FirstName: a game text key (NAME_..., character...) or any name as it is; null = template's
        "avatar": None,
        "items": [],                   # --list items; added over the template's items (same slot = replaced)
        "perks": [],                   # --list perks
        "perk_sets": {},               # a perk's Set values, e.g. {"PERK_MIRROR": {"Chance": "0.35"}} (a fight's own)
        "enchantments": [],            # custom match: item enchantments (--list perks: PERK_ITEM_SPECIAL_..._<SLOT>), at the top level's strength
        "tactic": "Standard",          # --list tactics (AI only)
        # custom match, human: true = picks gear in the dojo first (the template gives name, look, perks);
        # false = the champion as it is, straight into the fight. Game fight: the player always starts in the
        # dojo. Two players on one screen: no dojo
        "dojo": False,
        "power": None,                 # WarriorPower
        "attrs": {},                   # any Warrior attribute, e.g. {"WeaponDamage": 20}
        "align": [[0, 0]],             # AttributesAlign [[factor, shift], ...]; [[0,0]] = raw stats; null = template's
        "own": None,                   # the own fighter as saved after the dojo: {"items": [5 x "NAME@upgrade"],
                                       # "perks": ["PERK@level", ...] (the perk screen's)}; no dojo then
        "extra_xml": "",               # raw XML appended inside <Warrior>
    },
    "right": {                         # game fight: the game's opponents (only "human" applies; default false)
        "human": True, "template": "Man_Kungfu", "name": None, "avatar": None, "items": [], "perks": [], "perk_sets": {}, "enchantments": [],
        "tactic": "Standard", "dojo": False, "power": None, "attrs": {}, "align": [[0, 0]], "own": None, "extra_xml": "",
    },
}

MATCH = {"zone": "ZONE_T", "battle": "TEST", "fight": "1"}


class Progress:
    """Progress lines like "[ 42%  2:05] arena hunter_raid". Every step gets its own line, so the whole run
    stays readable. window() gives a part of the run (e.g. the Android import) a range of the percent;
    stage() and part() inside it count from 0 to 100 of that range."""
    def __init__(self):
        self.t0, self.base, self.lo, self.hi = time.time(), (0.0, 100.0), 0.0, 100.0

    def start(self):
        self.t0 = time.time()

    def window(self, lo, hi):
        self.base = (lo, hi)

    def stage(self, lo, hi, text):
        b0, b1 = self.base
        self.lo, self.hi = b0 + (b1 - b0) * lo / 100, b0 + (b1 - b0) * hi / 100
        self.say(self.lo, text)

    def part(self, i, n, text):
        """item i of n in the current stage"""
        self.say(self.lo + (self.hi - self.lo) * i / max(n, 1), text)

    def say(self, pct, text):
        t = int(time.time() - self.t0)
        print(f"[{pct:3.0f}% {t // 60:2d}:{t % 60:02d}] {text}", flush=True)


PROGRESS = Progress()


ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# sf2.js key ids: letters = ASCII code, arrows 133-136, , . / ; ' = 44 46 47 59 39, numpad 0-9 = 96-105
# controls: 1-8 directions (1 = up, clockwise), 9 punch, 10 kick, 11 ranged, 12 magic, 14 super
KEYS = [
    {"up": 87, "down": 83, "left": 65, "right": 68, "9": [70], "10": [71], "11": [82], "12": [84], "14": [81]},
    {"up": 133, "down": 136, "left": 134, "right": 135,
     "9": [44, 97], "10": [46, 98], "11": [47, 100], "12": [59, 101], "14": [39, 96]},
]


# =============================================================================== sf2.js patch
# Everything we add lives in globals named __sf2... or __SF2... (the minified game's names are short, so
# they cannot collide). __SF2 holds the match settings.
JS_HEADER = r"""
// ---------------------------------------------------------------- the match, set up in the browser
// __SF2DATA (sf2data.js): what the build read from the game files (defaults, item types, templates ...).
// The match comes from the menu or from server.py's match.js (window.__SF2_CONFIG; for two devices also
// window.__SF2_NETINFO = {relay, role}). __SF2 is everything the patches use, resolved from it.
var __SF2DATA=window.__SF2DATA;
// __sf2Merge and __sf2Check are in the menu (sf2menu/menu.js, loaded before the game)
function __sf2TemplateItems(name){              // a template's items, parents' included (a child replaces per slot)
  var T=__SF2DATA.templates,chain=[],out={};
  while(T[name]&&chain.indexOf(name)<0){chain.push(name);name=T[name][0]}
  chain.reverse().forEach(function(n){var it=T[n][1];for(var t in it)out[t]=it[t]});
  return out}
function __sf2Resolve(user){
  var D=__SF2DATA,c=__sf2Merge(D.defaults,user),slots=['Weapon','Ranged','Magic','Helm','Armor'];
  var net=!!window.__SF2_NETINFO;               // two devices: chosen in the menu (Host / Join), not in the match
  if(c.fight&&!(user.right&&'human' in user.right))c.right.human=false;   // a game fight: the game's AI by default
  var sides=[c.left,c.right],humans=[0,1].filter(function(i){return sides[i].human});
  var me=net?((window.__SF2_NETINFO||{}).role=='join'?1:0):humans.length==1?humans[0]:-1;   // the side at this screen
  var dojo=[0,1].map(function(i){return c.fight?i==0&&sides[0].human:sides[i].human&&!!sides[i].dojo}),
      hub=!!net&&humans.length>0||humans.length==1&&dojo[me];
  var bySlot=function(names,have){(names||[]).forEach(function(n){var t=D.types[n];t&&(have[t]=n)});return have};
  var dress=sides.map(function(s,i){            // what the dojo character starts in (only items the dojo shows)
    var have=c.fight?(i?{}:bySlot(s.items,{})):bySlot(s.items,s.template?__sf2TemplateItems(s.template):{});
    return slots.map(function(t){var n=have[t];return n&&D.dojo.indexOf(n)>=0?n:null})});
  var f=c.fight?c.fight.split('/'):[D.match.zone,D.match.battle,D.match.fight];
  return{cfg:c,zone:f[0],battle:f[1],fight:f[2],custom:!c.fight,humans:humans,net:!!net,
    lan:!!net&&humans.length==2,                // two players through the network: lockstep
    hub:hub,                                    // the game stays up between matches (the dojo, or waiting)
    skipDojo:hub&&!(me>=0&&dojo[me]),           // this screen's side is a champion: straight to the fight
    delay:c.lan_input_delay=='auto'?0:+c.lan_input_delay,language:c.language,eclipse:!!c.eclipse,
    difficulty:c.difficulty?D.difficulties.indexOf(c.difficulty):null,
    leftItems:c.fight&&c.left.items.length?slots.map(function(t){return bySlot(c.left.items,{})[t]||null}):[],
    leftTactic:c.left.tactic,dress:dress,
    profile:D.profile+JSON.stringify([c.fight,c.difficulty]),   // a new profile when its setup changes
    keys:D.keys,packs:D.packs,start:D.start,superPerks:D.superPerks,superMods:D.superMods,itemSets:D.itemSets||{},
    probeGear:D.probeGear,maxLevel:D.maxLevel,
    relay:(window.__SF2_NETINFO||{}).relay||location.hostname+':8081',role:(window.__SF2_NETINFO||{}).role||'host'}}
function __sf2Esc(v){return String(v).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function __sf2WarriorXml(s){                    // a side of a custom match as a <Warrior>
  var a=[],av=s.avatar||(__SF2DATA.avatarFix||{})[s.template];   // a template without its picture: the classic shadow
  s.template&&a.push(['Template',s.template]);s.name&&a.push(['FirstName',/^(NAME_|character)\w*$/.test(s.name)?s.name:'SF2TEXT_'+s.name.replace(/[{}%]/g,'')]);av&&a.push(['Avatar',av]);   // name: a game text key (a character's) or itself
  s.human?a.push(['Controlled','1'],['NotAI','1']):a.push(['Tactic',s.tactic]);
  s.power!=null&&a.push(['WarriorPower',s.power]);for(var k in s.attrs)a.push([k,s.attrs[k]]);
  var inner='';
  s.items.length&&(inner+='<Items>'+s.items.map(function(i){return'<Item Name="'+__sf2Esc(i)+'"/>'}).join('')+'</Items>');
  var en=s.enchantments||[],asp=__SF2DATA.enchAspect;   // enchantments: as the game gives them, perks with their strength
  var ps=s.perk_sets||{},set=function(i){var v=ps[i],a='';for(var k in v)a+=' '+k+'="'+__sf2Esc(v[k])+'"';return a?'<Set'+a+'/>':''};   // a fight's own values
  (s.perks.length||en.length)&&(inner+='<Perks>'+s.perks.map(function(i){var t=set(i);return'<Perk Name="'+__sf2Esc(i)+'"'+(t?'>'+t+'</Perk>':'/>')}).join('')+
    en.map(function(i){return'<Perk Name="'+__sf2Esc(i)+'"><Set Aspect="'+asp+'"/></Perk>'}).join('')+'</Perks>');
  s.align!=null&&(inner+='<AttributesAlign>'+s.align.map(function(d){return'<Delta Factor="'+d[0]+'" Shift="'+d[1]+'" Priority="1"/>'}).join('')+'</AttributesAlign>');
  return'<Warrior '+a.map(function(x){return x[0]+'="'+__sf2Esc(x[1])+'"'}).join(' ')+'>'+inner+s.extra_xml+'</Warrior>'}
function __sf2BattleXml(c){                     // the custom match's <Battle> in zone ZONE_T
  var m=__SF2DATA.match,fa='';for(var k in c.fight_attrs)fa+=' '+k+'="'+__sf2Esc(c.fight_attrs[k])+'"';
  return'<Battle Name="'+m.battle+'" Type="'+__sf2Esc(c.battle_type)+'" X="-100" Y="-40" Location="'+__sf2Esc(c.location)+
    '" Music="'+__sf2Esc(c.music)+'"><Fight Name="'+m.fight+'" Power="0" Rounds="'+(c.rounds|0)+'" RoundTime="'+(c.round_time|0)+
    '" Replays="1"'+fa+'><Rewards><Reward Money="0" Exp="0" PrizeBase="1"/><Reward Money="0" Exp="0"/></Rewards><Warriors>'+
    __sf2WarriorXml(c.left)+__sf2WarriorXml(c.right)+'</Warriors><Rules>'+c.rules.join('')+'</Rules></Fight></Battle>'}
// every XML file the game reads goes through Ja.pi (patch "XML files"): a custom match replaces zone ZONE_T
function __sf2Xml(name,text){
  if(!__SF2.custom||text==null||!/stages\.xml$/.test(name||''))return text;
  return text.replace(/(<Zone Name="ZONE_T">)[\s\S]*?(<\/Zone>)/,function(x,a,b){return a+__sf2BattleXml(__SF2.cfg)+b})}
var __SF2=__sf2Resolve(window.__SF2_CONFIG||{});
// back to the menu: a button while in the dojo (two players on one screen: the address with ?menu)
setInterval(function(){
  var A=window.__sf2API,b=document.getElementById('sf2menu'),show=!!(A&&A.ya&&A.ya.F()&&A.ya.F().Ud.ag==3)&&!__sf2Look&&!window.__SF2_LOOKBOOT;
  if(!b&&document.body){b=document.createElement('a');b.id='sf2menu';b.textContent='MATCH MENU';b.href='?menu';
    b.onclick=function(e){if(window.__sf2OpenMenu&&!__SF2.lan){e.preventDefault();__sf2OpenMenu()}};   // over the dojo
    b.style.cssText='position:fixed;bottom:6px;left:50%;transform:translateX(-50%);z-index:99999;font:bold 12px sans-serif;'+
      'color:#f3c66b;background:rgba(0,0,0,.6);border:1px solid #f3c66b;border-radius:12px;padding:4px 12px;text-decoration:none';
    document.body.appendChild(b)}
  b&&(b.style.display=show?'':'none')},500);
// the game's touch controls (stick, buttons, magic and Super charge): shown when one player uses this
// screen. Two players on one screen would cover each other's; AI vs AI needs none
var __SF2_HIDE_TOUCH=!(__SF2.lan||__SF2.humans.length==1);
var __sf2Kb=null;                              // the keyboard device (set by the game)

// The game's own error handler (EA) stopped the main loop for good on any uncaught error or rejected
// promise, so the screen froze. Now the error is shown and sent to the server window, and the game keeps
// running (the loop asks for its next frame before it runs the current one).
var __sf2Errs={n:0,seen:{},box:null};
function __sf2Report(kind,msg,where){                          // -> "[game-<kind>]" in the server window
  if(window.SF2Native)return SF2Native.log(kind,msg+(where?' at '+where:''));   // the APK: Android's log
  try{fetch('/sf2-log',{method:'POST',body:JSON.stringify({kind:kind,msg:msg,where:where||'',ua:navigator.userAgent}),keepalive:true})}catch(x){}}
function __sf2Error(ev){
  var e=ev&&(ev.error||ev.reason),msg=(e&&e.message)||(ev&&ev.message)||String(e||ev);
  var where=(e&&e.stack||'').split('\n').slice(0,4).join(' | ')||((ev&&ev.filename)||'')+':'+((ev&&ev.lineno)||'');
  var k=msg+'@'+where;__sf2Errs.n++;
  if(!__sf2Errs.seen[k]){__sf2Errs.seen[k]=1;console.error('[sf2] game error:',msg,where);
    __sf2Report('error',msg,where)}
  var b=__sf2Errs.box;
  if(!b&&document.body){b=__sf2Errs.box=document.createElement('div');
    b.style.cssText='position:fixed;top:4px;left:4px;max-width:60%;z-index:99999;font:11px monospace;color:#fff;'+
      'background:rgba(150,0,0,.85);padding:4px 8px;border-radius:4px;pointer-events:none;white-space:pre-wrap';
    document.body.appendChild(b)}
  b&&(b.textContent='game error ('+__sf2Errs.n+'): '+msg+'\n'+where.slice(0,300))}

// phones: a normal tab keeps the address bar. Every touch outside fullscreen asks for fullscreen and
// landscape (browsers allow it only during a user gesture; iPhone Safari has no API for it)
if(typeof matchMedia!=='undefined'&&matchMedia('(pointer: coarse)').matches)
  window.addEventListener('pointerup',function(){
    var d=document.documentElement,fs=document.fullscreenElement||document.webkitFullscreenElement;
    if(fs)return;
    var r=d.requestFullscreen||d.webkitRequestFullscreen;
    if(r){var p=r.call(d,{navigationUI:'hide'});
      p&&p.then&&p.then(function(){screen.orientation&&screen.orientation.lock&&screen.orientation.lock('landscape').catch(function(){})},function(){})}},!0);

function __sf2BindKeys(kb){                    // key set 0 -> binding index 0, key set 1 -> index 1
  // one player: the game's own keys (K L O P, J, Q - the letters on the control panel) stay as well
  if(__SF2.humans.length!=1||__SF2.lan)kb.aM=kb.aM.filter(function(b){return b.index!==0});
  __SF2.keys.forEach(function(K,i){
    var U=K.up,D=K.down,R=K.right,L=K.left;
    kb.He(2,i,U,R);kb.He(8,i,U,L);kb.He(4,i,D,R);kb.He(6,i,D,L);
    kb.He(1,i,U);kb.He(5,i,D);kb.He(3,i,R);kb.He(7,i,L);
    [9,10,11,12,14].forEach(function(c){K[c].forEach(function(k){kb.He(c,i,k)})});
  })}

function __sf2FighterBindings(Oc,yy,F){        // input device -> fighter (0 left, 1 right); yy(fighter, type, index)
  var h=__SF2.humans;Oc.UG.length=0;           // type: 0 touch, 1 keyboard, 2 gamepad
  if(!__sf2IsMatch(F))h=[0];                   // the dojo: the player is fighter 0, every device
  else if(__SF2.lan){Oc.UG.push(new yy(0,1,0),new yy(1,1,1));Oc.ida=-1;return}   // the lockstep drives both key sets
  if(h.length==2)Oc.UG.push(new yy(0,1,0),new yy(1,1,1),new yy(0,0,0),new yy(0,2,0));
  else if(h.length==1)Oc.UG.push(new yy(h[0],1,0),new yy(h[0],1,1),new yy(h[0],0,0),new yy(h[0],2,0));
  else Oc.UG.push(new yy(-1,1,0));             // AI vs AI: input goes nowhere
  Oc.ida=-1}

// the light under the left fighter: hidden while that fighter is invisible (e.g. Lynx's cloak), unless
// this screen belongs to that fighter's player alone; otherwise the other player would see where it is
function __sf2Light(L){var F=window.__sf2API&&__sf2API.fight,m=F&&(__sf2Lan&&__sf2Lan.side==1?F.pb:F.zb),   // under this device's fighter
  hide=!!(m&&m.go&&m.go.node.ln==1&&__sf2IsMatch(F)&&!(__sf2Lan||__SF2.humans.length==1&&__SF2.humans[0]==0));
  if(hide){L.R(!1);L.__sf2Hid=1}else if(L.__sf2Hid){L.R(!0);L.__sf2Hid=0}
  return!hide}

// An invisible fighter (ModInvisibility: Lynx's cloak, the Invisibility Super) is hidden on every screen,
// except the screen of its own player, which shows it as a ghost (alpha .35). In story fights the game's
// player is the left one.
function __sf2Cloak(m,show){var F=window.__sf2API&&__sf2API.fight,i=F?(m==F.zb?0:m==F.pb?1:-1):-1;if(i<0)return!1;
  var own=__sf2IsMatch(F)?(__sf2Lan?__sf2Lan.side==i:__SF2.humans.length==1&&__SF2.humans[0]==i):i==0,
    al=!show&&own?.35:null;
  [m.fX].concat(m.eca.mv).forEach(function(q){q.__sf2A=al;q.e&&q.e.color&&(q.e.color.w=al==null?1:al)});
  m.__sf2Seen=show?null:m.pa.Ke().ma.x;                // where the AI last saw it
  if(al==null)return!1;m.go.setActive(!0);return!0}

// An AI does not see an invisible opponent: its tactics (distance, Ql) and its moves' Tactics conditions
// (he.At, while __sf2TacOn) use the place where the opponent was last seen (__sf2Seen). A human sees what
// the screen shows. A move's own Conditions (e.g. a throw's reach) stay real.
var __sf2TacOn=false;
function __sf2Human(m){var F=window.__sf2API&&__sf2API.fight,i=F?(m==F.zb?0:m==F.pb?1:-1):-1;
  return __sf2IsMatch(F)?__SF2.humans.indexOf(i)>=0:i==0}
function __sf2SeenX(me,m,p){return m&&me&&m!==me&&m.__sf2Seen!=null&&!__sf2Human(me)?m.__sf2Seen:p.ma.x}
function __sf2SeenAt(h,a,c){var me=a.Cr,F=__sf2API.fight,b=h.xfa(a),
    m=b===a.Qb?me:b===a.Qd&&F?(me==F.zb?F.pb:F.zb):null;
  if(!__sf2TacOn||!m||m===me||m.__sf2Seen==null||__sf2Human(me))return c;
  return new c.constructor(c.x+m.__sf2Seen-m.pa.Ke().ma.x,c.y,c.z,1)}

// ... and learns where it is when it hits (v.MAa: the damage of a hit, attacker's parameters b)
function __sf2Felt(b){var F=window.__sf2API&&__sf2API.fight;if(!F||!F.zb)return;
  [F.zb,F.pb].forEach(function(m){m&&m.parameters===b&&m.__sf2Seen!=null&&(m.__sf2Seen=m.pa.Ke().ma.x)})}

function __sf2Ours(f){return!!(f&&f.Ob&&f.Ob.Qe==__SF2.zone&&f.Ob.Ve==__SF2.battle)}  // fight data of our match
function __sf2Custom(f){return __SF2.custom&&__sf2Ours(f)}                             // ... of a custom match
function __sf2IsMatch(F){return __sf2Ours(F&&F.Da)}                                    // a fight object of it

// who fights (v.XJa): d = the left fighter (in the game: the player), e = the opponents.
// Custom match: both sides come from the XML (left = the first warrior). Game fight: the player and the
// opponents stay the game's; only who controls them changes (tactic(name) = the game's P.Hfa)
function __sf2Sides(F,d,e,tactic){
  if(!__sf2Ours(F))return[d,e];
  if(__SF2.custom){if(e.length>1){d=e[0];d.qb=!0;e=e.slice(1)}return[d,e]}
  if(__SF2.humans.indexOf(0)<0){d.Jj=!0;d.Gu=!1;d.Hc=tactic(__SF2.leftTactic)}   // the AI plays the player's character
  if(__SF2.humans.indexOf(1)>=0)e.forEach(function(w){w.Gu=!0;w.Jj=!1});         // a human plays the opponents
  return[d,e]}

// ---------------------------------------------------------------- scenes
// Direct mode (two players on one screen, or AI vs AI): the first scene request at boot (0 -> 3, the dojo)
// goes into the match instead; leaving the match reloads the page for a new one.
// Hub mode: the game starts in the dojo (every item, no tutorial). The dojo's fight or map button
// (scene 5) starts the match; after the match the game returns to the dojo.
var __sf2Entered=false;
var __sf2Hub={on:!!__SF2.hub,inMatch:false};
function __sf2EnterMatch(){
  if(__sf2Hub.inMatch)return true;
  var A=window.__sf2API,z=A.p.OD(__SF2.zone),bt=z&&z.JQ(__SF2.battle);
  var f=(bt?bt.Wz():[]).filter(function(x){return x.name==__SF2.fight})[0];
  if(!f){console.error('[sf2] match fight not found');return false}
  __sf2Entered=true;__sf2Hub.inMatch=true;__sf2Hub.loc=f.location;
  var t0=Date.now(),told=false;
  function stuck(why){                           // once, if entering takes long: what it is waiting for
    if(told||Date.now()-t0<20000)return;told=true;var sc=A.na.Og();
    __sf2Report('info','entering the match takes long: '+why+' (scene '+A.ya.F().Ud.ag+', '+(sc&&sc.constructor.name)+
      ', transition '+!!(sc&&sc.Wr&&sc.Wr.transition)+')')}
  setTimeout(function check(){if(!__sf2Hub.inMatch)return;var sc=A.na.Og();
    if(sc&&sc.constructor===A.ei)return;
    if(__sf2Lan&&__sf2Lan.myGo&&!__sf2Lan.items())return setTimeout(check,1000);   // waiting for the other player: fine
    stuck('the fight scene did not open');if(!told)setTimeout(check,1000)},1000);
  (function go(){
    if(__sf2Lan&&__sf2Lan.side<0){stuck('no LAN seat yet');return setTimeout(go,100)}  // LAN: side first
    var sc=A.na.Og();                 // a scene transition (e.g. the dojo fading in) still running: the scene
    if(!sc||sc.Wr.transition){stuck('a scene transition is running');return setTimeout(go,100)}  // manager would drop the new scene (mc.oba)
    if(__sf2Lan){                                                // LAN: send my items, wait for the other's
      if(!__sf2Lan.myGo)__sf2Lan.go(__SF2.skipDojo?null:A.p.o.Ca);   // a champion: its own items
      if(!__sf2Lan.items()){stuck('the opponent has not pressed fight');return setTimeout(go,100)}}
    A.uy.Da=null;A.v.SF=false;        // v.Im: skips the fight entered last time / takes the map's "retry" path
    __sf2Calibrate();                 // "difficulty" in a game fight without the dojo
    A.p.o.JN=true;                    // the game's "unlimited energy": a fight's Power costs no energy
    A.p.o.ci=!!__SF2.eclipse;         // the profile's Eclipse mode: Eclipse="0|1" rules and AttributesAlign deltas
    A.v.Im(f,false,null,false,false,true);                       // the game's own "enter fight"
    // LAN: the fight's saved random seeds differ per browser, so new ones come from the common seed,
    // which is also the game generator's state from here on
    __sf2Lan&&f.Xc&&(__sf2Lan.reseed(A.J,A.Da,0),f.Xc.xea())})();
  return true}
function __sf2Nav(ya,a){
  var cur=ya.Ud.ag;
  if(!__sf2Hub.on){
    if(!__sf2Entered&&cur==0&&a==3)return __sf2EnterMatch();
    if(__sf2Entered&&cur==6&&a!=6){             // match over: the menu's match -> the menu (its choices kept), match.json -> again
      sessionStorage.getItem('sf2-config')?location.href=location.pathname+'?menu':location.reload();return true}
    return false}
  if(__sf2Hub.inMatch&&cur==6&&a!=6&&__SF2.net){location.href=location.pathname+'?menu';return true}   // network: the menu (the lobby again)
  if(__sf2Hub.inMatch&&cur==6&&a!=6){                           // match over -> back to the dojo
    __sf2Hub.inMatch=false;__sf2Lan&&__sf2Lan.reset();
    if(a==5){setTimeout(function(){ya.xp(3)},0);return true}    // "OK" goes to the map; the hub has none
    return false}                                               // dojo / shop (STORE) / profile as usual
  if(a==5&&cur!=6)return __sf2EnterMatch();                     // the dojo's fight button
  if(a==3&&__sf2Lan&&__sf2Lan.side<0){setTimeout(function(){ya.xp(3)},100);return true}   // LAN: side first
  if(__sf2Look&&cur==3&&a!=3)return true;                       // looking at a form: the dojo is only a picture
  if(a==3&&!__sf2Hub.dressed){__sf2Hub.dressed=true;__sf2TryUndo();   // first time: match.json's items (Try: the form)
    window.__SF2_LOOKBOOT?__sf2Look&&__sf2LookDress():(__sf2Calibrate(),__sf2Dress())}
  // a champion (or the game's opponents over LAN): nothing to pick in the dojo, straight to the fight,
  // where it waits for the other player's "fight"
  if(a==3&&__SF2.skipDojo)return __sf2EnterMatch();
  return false}

// the dojo character starts in the items match.json gives this side (custom match: the template's items
// plus "items"; game fight: "left.items"), equipped the way the inventory does it (p.o.ya.ip).
// Only items the dojo inventory can show (__SF2.dress holds no others)
function __sf2Dress(){
  var A=window.__sf2API,side=__sf2Lan?__sf2Lan.side:__SF2.humans[0],names=__SF2.dress[side]||[];
  names.forEach(function(n){var it=n&&A.p.items.$b(n);it&&!(__sf2Diff.level&&it.Ff>__sf2Diff.level)&&A.p.o.ya.ip(it,true)})}

// ---------------------------------------------------------------- difficulty
// The game rates a fight for the map's difficulty icon: v.lBa(fight) = Tz(player, opponents) = q9a, the last
// opponent's rating divided by the player's (RatingEvaluation: expected damage per hit type, with the same
// damage formula). It is compared with DifficultyEvaluation's thresholds (Yc.pD: 0, .82, 1.3, 2.6, 5.2 =
// very easy .. very hard). "difficulty" aims at the middle of its band (geometric).
//   game fight: the player's level L whose best gear (every slot up to L, upgraded to L's last step) rates
//     closest. The profile becomes level L: items above L leave the inventory, upgrades above L come down.
//   custom match: the AI side's attack and defense (what WarriorPower adds to, v.Fv) shift until the AI
//     side vs the human side (with the human's gear) rates at the target
var __sf2Diff={level:0,done:false};
function __sf2DiffBand(){                      // [low, high) of the chosen difficulty's rating
  var t=window.__sf2API.Yc.pD.map(function(c){return c.second}),i=__SF2.difficulty;
  return[i==0?0:t[i],i>=t.length-1?Infinity:t[i+1]]}
function __sf2DiffTarget(){                      // its middle (geometric)
  var b=__sf2DiffBand();return b[0]==0?b[1]/Math.SQRT2:b[1]==Infinity?b[0]*Math.SQRT2:Math.sqrt(b[0]*b[1])}
function __sf2DiffPick(cands){                   // in the band the nearest to its middle, else the nearest
  var b=__sf2DiffBand(),T=Math.log(__sf2DiffTarget()),best=null,bs=1e9;
  cands.forEach(function(c){var s=Math.abs(Math.log(c.r)-T)+(c.r>=b[0]&&c.r<b[1]?0:100);if(s<bs){bs=s;best=c}});
  return best}
function __sf2ItemAt(n,L,k){                   // item n at level L's upgrade step k (or the nearest below)
  var p=window.__sf2API.p;for(;k>=0;k--){var it=__sf2Item(p,n+'@'+(L*100+k*10));if(it&&it.Xg==L*100+k*10)return it}
  return null}
function __sf2BestGear(L,k){                     // per slot: the item for level L at step k, or the start item
  // (gear of level L worn by a player of a higher level: L is the gear's level here)
  var p=window.__sf2API.p;return __SF2.probeGear.map(function(list,i){
    var n=null;list.forEach(function(x){x[1]<=L&&(n=x[0])});
    return n&&__sf2ItemAt(n,L,k)||p.items.$b(__SF2.start[i])})}
function __sf2GearRating(f,ops,L,gear){          // the player at level L in this gear
  var A=window.__sf2API,o=A.p.o,old=o.bb();o.GL(L);
  var c=A.v.lw().clone();gear.forEach(function(it,i){it&&c.lk(__sf2Slots[i],it.clone())});
  c.mM&&c.mM();c.Om();var r=f.Tz(c,ops);o.GL(old);return r}
function __sf2Calibrate(){                       // game fight: pick the level, cap the profile to it
  if(__sf2Diff.done||__SF2.custom||__SF2.difficulty==null)return;__sf2Diff.done=true;
  var A=window.__sf2API,o=A.p.o,z=A.p.OD(__SF2.zone),bt=z&&z.JQ(__SF2.battle);
  var f=(bt?bt.Wz():[]).filter(function(x){return x.name==__SF2.fight})[0];if(!f)return;
  __sf2Lan&&__sf2Lan.reseed(A.J,A.Da,4);         // LAN: random opponent groups -> the same on both browsers
  var ops=A.v.QQ(f.kt),cands=[],top=0;
  // player levels from the top down; for each, its gear from the best (own level, last upgrade step)
  // down through the steps and up to 3 gear levels below. The rating grows as the gear gets weaker. Below
  // the levels the upgrade tables cover it stops growing, so the scan ends there
  for(var L=__SF2.maxLevel;L>=1;L--){var prev=0,first=true;
    lv:for(var G=L;G>=Math.max(1,L-3);G--)for(var k=4;k>=0;k--){
      var g=__sf2BestGear(G,k);if(g[0].Xg!=G*100+k*10&&k)continue;   // no such step for this level
      var r=__sf2GearRating(f,ops,L,g);if(!isFinite(r)||r<prev*0.98)break lv;
      if(first){if(r<top*0.98)L=0;top=r;first=false;if(!L)break lv}
      prev=r;cands.push({L:L,G:G,k:k,r:r,g:g})}}
  var best=__sf2DiffPick(cands);if(!best)return;
  var T=Math.log(__sf2DiffTarget());
  __sf2Diff.level=best.L;o.GL(best.L);
  o.ya.items.slice().forEach(function(e){var it=e.ib;if(!it||__sf2Slots.indexOf(it.type)<0)return;
    if(it.Ff>best.L){o.ya.removeItem(e);return}   // the game shows items up to the player's level
    e.Ge>best.L*100+40&&e.Zp(best.L*100);e.Eu(best.L)});   // upgrades only up to the player's level
  best.g.forEach(function(it,i){                 // worn: the gear the level was rated with; match.json's
    var e=it&&o.ya.Uj(it.name);                  // items go on over it (__sf2Dress)
    e&&(e.Zp(it.Xg),e.Eu(best.L));
    o.ya.ip(e?e.ow()||e.ib:A.p.items.$b(__SF2.start[i]),true)});
  o.save();
  var b=__sf2DiffBand(),inb=best.r>=b[0]&&best.r<b[1];
  __sf2Report('info','difficulty '+__SF2.difficulty+': player level '+best.L+', worn gear of level '+best.G+
    ' upgrade step '+best.k+' (rating '+best.r.toFixed(2)+', target '+Math.exp(T).toFixed(2)+
    (inb?'':'; the nearest this fight allows')+'; better gear or upgrades = easier)')}
function __sf2BotStrength(sc,sides){             // custom match: shift the AI side to the target
  if(__SF2.difficulty==null||!__SF2.custom)return;
  var A=window.__sf2API,h=__SF2.humans.length==1?__SF2.humans[0]:0,me=sides[h],bot=sides[1-h];
  var names=A.v.Fv.map(function(a){return a.name}),base={};
  bot.Om();names.forEach(function(n){var r={G:0};bot.attributes.get(n,r);base[n]=r.G});
  // a warrior's attribute comes from pM when set there (XML attributes, WarriorPower), else from its
  // items and level (Om), so the shift goes into pM, like WarriorPower's
  function at(d){names.forEach(function(n){bot.pM.set(n,base[n]+d)});bot.Om();return sc.Da.q9a(me,bot)}
  var T=__sf2DiffTarget(),lo=-5000,hi=5000;
  for(var k=0;k<50;k++){var m=(lo+hi)/2;at(m)<T?lo=m:hi=m}
  var r=at(Math.round(lo));
  __sf2Report('info','difficulty '+__SF2.difficulty+': AI side '+(lo>=0?'+':'')+Math.round(lo)+
    ' attack/defense (rating '+r.toFixed(2)+', target '+T.toFixed(2)+')')}

// the player's dojo gear goes onto the human side(s) right before the fighters are created.
// LAN: both devices put the same items on the same sides (lists exchanged in __sf2Lan.go)
var __sf2Slots=['Weapon','Ranged','Magic','Helm','Armor'];
// what a character wears: "NAME@upgrade" per slot. The worn item is the upgraded copy the inventory made
// (Xg = 100 x level + 10 x step); the catalog's p.items.$b(name) is the item before any upgrade, so the
// upgrade travels with the name
function __sf2Equip(zc){return __sf2Slots.map(function(t){var it=zc&&zc.Gd(t);return it?it.name+'@'+it.Xg:null})}
function __sf2Item(p,n){                        // "NAME@upgrade" -> the item at that upgrade (the game's Fu)
  var a=n.split('@'),it=p.items.$b(a[0]),u=+a[1];
  if(it&&a.length>1&&u!=it.Xg){var c={G:null},d={G:null};it.Fu(1e6,u,c,d);c.G&&(it=c.G)}
  return it}
// a slot the player did not change in the dojo (still the new player's Fists, Head, Body ...) keeps the
// item of the side's template and "items"
// exact: every slot as given (the player's own character in a game fight)
function __sf2PutOn(zc,names,p,exact){
  names.forEach(function(n,i){var it=n&&(exact||n.split('@')[0]!=__SF2.start[i])&&__sf2Item(p,n);
    it&&zc.lk(__sf2Slots[i],it.clone())});
  zc.mM&&zc.mM()}
// The menu's "Try": the chosen form in the dojo, with its items, voice, item specials and Super; magic
// and Super charge in 3/4 s. The dojo's menus are hidden and its scene changes blocked; the touch controls
// stay. The menu starts the game for it if needed (window.__SF2_LOOKBOOT; the form waits in
// __sf2LookPending). The player's own gear, voice and perks are kept in sessionStorage and come back when
// looking ends (__sf2TryUndo; also before a match and at the next dojo entry).
var __sf2Look=window.__sf2LookPending||null;      // {items, voice, perks (item specials)} looked at, or null
// When looking ends behind the paused menu, the dojo is rebuilt with the player's own gear only once the
// game runs again (__sf2Pause(false)); rebuilt at once, its stance sound would play behind the menu
var __sf2LookReload=false;
function __sf2LookAt(look){var A=window.__sf2API,dojo=A&&A.p&&A.ya&&A.ya.F()&&A.ya.F().Ud.ag==3;
  __sf2Look=look||null;
  look?dojo&&__sf2LookDress():(__sf2LookParams(A),__sf2TryUndo());
  look?(__sf2LookReload=false,dojo&&A.ya.F().xp(3)):__sf2LookReload=dojo;__sf2LookOn||__sf2LookFrame()}
function __sf2LookDress(){var A=window.__sf2API;
  sessionStorage.getItem('sf2-try')||sessionStorage.setItem('sf2-try',JSON.stringify(__sf2Equip(A.p.o.Ca)));
  __sf2PutOn(A.p.o.Ca,__sf2Look.items,A.p,true);__sf2LookParams(A);__sf2LookOn||__sf2LookFrame()}
function __sf2LookParams(A){                      // the player's voice and perks (each dojo fighter is a copy: v.lw()): the form's / its own again
  var P=A&&A.v&&A.v.lw(),L=__sf2Look;if(!P)return;
  if(P.__sf2Own){P.voice=P.__sf2Own.voice;P.IK.length=0;Array.prototype.push.apply(P.IK,P.__sf2Own.IK);delete P.__sf2Own}
  if(!L)return;
  P.__sf2Own={voice:P.voice,IK:P.IK.slice()};L.voice&&(P.voice=L.voice);
  L.perks.forEach(function(n){var k=A.v.Vg.tn(n);k&&P.IK.indexOf(k)<0&&P.IK.push(k)})}
var __sf2LookOn=false;                            // the frame loop below runs
function __sf2LookFrame(){var A=window.__sf2API,z=A&&A.za&&A.za.instance,f=A&&A.fight,m=f&&f.zb;
  z&&z.node.R(!__sf2Look);                        // the dojo's menus: hidden while looking
  if(m&&__sf2Look&&!m.__sf2Perks){m.__sf2Perks=1;var Q=m.parameters;   // the dojo builds its fighter's moves from
    __sf2Look.perks.forEach(function(n){var k=A.v.Vg.tn(n);k&&Q.Oa.indexOf(k)<0&&Q.Oa.push(k)});   // Oa (sa.cAa(..,true)),
    m.enb()}                                      // empty after the copy: the form's perks in it, the moves again
  if(m&&__sf2Look){
    m.fh==0&&(m.bxa(1/45),m.XA());                // magic: a charge (bullet) in 3/4 s
    m.Ja.tu<m.Ja.JU&&(m.Ja.tu=Math.min(m.Ja.JU,m.Ja.tu+m.Ja.JU/45))}   // Super: the game's own charge, which in a fight fills
                                                  // during a round ($Ja drops control 14 below it) - the dojo has no round
  __sf2LookOn=!!__sf2Look;__sf2LookOn&&requestAnimationFrame(__sf2LookFrame)}
// a round's end type for two players (patch "two-player round result"): a time-out without a judging rule
// (3) or a double knockout (0) -> 6, the PvP judgement by health
function __sf2Judge(F){var t=F.ty;return __SF2.humans&&__SF2.humans.length==2&&F.Zu==null&&
  (t==3||t==0&&F.kc.jd<=0&&F.Zb.jd<=0)?6:t}
// the game behind the menu (over the dojo, after a Try): stopped - no frames, no sound
function __sf2Pause(on){var I=window.__sf2API&&__sf2API.J&&__sf2API.J.I;if(!I||!I.Nc)return;
  // what the game's pause for a hidden tab does (pause: loop stopped, lS: sound off; resume, M6: both back),
  // called directly: the platform's Kp / Np do nothing where the portal lacks "visibilitychange" (the network
  // and the app), which kept the dojo running behind the menu. And no input
  if(on){I.gga().KF(!1);I.pause();I.lS()}else if(!I.Nc.active){I.gga().KF(!0);I.ed().reset();I.resume();I.M6()}
  on||setTimeout(function(){var A=window.__sf2API;   // a Try that ended: the dojo with the player's gear (none if a new Try began)
    if(__sf2LookReload&&!__sf2Look){__sf2LookReload=false;A.ya.F().Ud.ag==3&&A.ya.F().xp(3)}},0)}
function __sf2TryUndo(){var A=window.__sf2API,b=sessionStorage.getItem('sf2-try');if(!b||!A||!A.p)return;
  sessionStorage.removeItem('sf2-try');__sf2PutOn(A.p.o.Ca,JSON.parse(b),A.p,true);A.p.o.save&&A.p.o.save()}
function __sf2Prepare(sc,p){                     // scene state 1; false = wait
  if(!__sf2Ours(sc.Da)||sc.__sf2Done)return true;
  var sides=[sc.OA.G,sc.yf.G[0]];
  if(!__SF2.custom){                             // game fight: the player is the player, opponents are the game's
    if(__sf2Lan){var n=__sf2Lan.items();if(!n)return false;__sf2PutOn(sides[0],n[0],p,true);   // the player's gear
      var pk=__sf2Lan.perks[0];pk&&(sides[0].cF.length=0,pk.forEach(function(x){var k=__sf2PerkOf(x);k&&sides[0].cF.push(k)}))}   // and perk-screen perks: the left player's, on both
    else if(!__sf2Hub.on&&__SF2.leftItems.length)__sf2PutOn(sides[0],__SF2.leftItems,p,true)}   // no dojo: left.items
  else if(!__sf2Hub.on);
  else if(__sf2Lan){var n=__sf2Lan.items();if(!n)return false;     // null: a champion, as the XML has it
    n[0]&&__sf2PutOn(sides[0],n[0],p);n[1]&&__sf2PutOn(sides[1],n[1],p)}
  else{var me=sides[__SF2.humans[0]];__sf2PutOn(me,__sf2Equip(p.o.Ca),p);   // the dojo's gear, and the perks chosen on its
    p.o.Ca.cF.forEach(function(k){me.cF.indexOf(k)<0&&me.cF.push(k)});__sf2SaveOwn(p.o.Ca)}   // perk screen; kept as the own fighter
  __SF2.custom&&[__SF2.cfg.left,__SF2.cfg.right].forEach(function(s,i){var o=s.own;if(!o||s.dojo)return;   // a saved own fighter: no dojo,
    __sf2PutOn(sides[i],o.items||[],p,true);(o.perks||[]).forEach(function(x){var k=__sf2PerkOf(x);k&&sides[i].cF.push(k)})});   // the same on both devices
  sides.forEach(__sf2Sets);
  __sf2BotStrength(sc,sides);
  sc.__sf2Done=true;return true}

// A perk chosen on the dojo's perk screen as text, "NAME@level": each upgrade level is a perk of its own
// (v.Vg.pT, matched on name and Uc as the perk screen does), so a name alone would lose the upgrades
function __sf2PerkKey(k){return k.name+(k.Uc!=null?'@'+k.Uc:'')}
function __sf2PerkOf(s){var a=String(s).split('@'),V=__sf2API.v.Vg;
  if(a.length>1)for(var i=0,P=V.pT;i<P.length;i++)if(P[i].name==a[0]&&String(P[i].Uc)==a[1])return P[i];
  return V.tn(a[0])}
// the own fighter after the dojo (gear, perk-screen perks), for the menu: localStorage sf2-own (it adds
// the name and picture)
function __sf2SaveOwn(zc){try{var o=JSON.parse(localStorage.getItem('sf2-own')||'{}');
  o.items=__sf2Equip(zc);o.perks=zc.cF.map(__sf2PerkKey);localStorage.setItem('sf2-own',JSON.stringify(o))}catch(e){}}
// an item set's bonus (Karcer: Predator's Fury) for a fighter wearing all of its items. Android gives it;
// the web engine reads item sets but never gives the bonus (list.xml ItemSet DefaultComboPerk; __SF2.itemSets)
function __sf2Sets(zc){var S=__SF2.itemSets||{},w=__sf2Slots.map(function(t){var it=zc&&zc.Gd(t);return it&&it.name});
  for(var k in S)if(S[k].every(function(n){return w.indexOf(n)>=0})){var pk=__sf2API.v.Vg.tn(k);
    pk&&zc.cF.indexOf(pk)<0&&zc.cF.push(pk)}}

// ---------------------------------------------------------------- Super button
// The game's touch controls have punch, kick, ranged and magic only; control 14 (Super: boss abilities like
// PERK_MINE, PERK_INVISIBILITY) is only on the keyboard (Q). A fifth button sends 14 the way the others
// send theirs (Kia -> Ue(14); LAN: the lockstep's touch bit). It shows while the local fighter has a perk
// that unlocks a Super move (__SF2.superPerks). Its ring shows the Super's recharge, a mod on the fighter
// (e.g. MineRecharge; __SF2.superMods): frames done (Qv) of all (bg); no mod = ready
function __sf2SuperModel(F){
  var side=!__sf2IsMatch(F)?0:__sf2Lan?__sf2Lan.side:__SF2.humans.length?__SF2.humans[0]:-1;
  return side==0?F.zb:side==1?F.pb:null}
function __sf2HasSuper(m){
  var k=m&&m.parameters&&m.parameters.IK||[];
  for(var i=0;i<k.length;i++)if(k[i]&&__SF2.superPerks.indexOf(k[i].name)>=0)return true;return false}
function __sf2SuperCharge(F,m){                 // 0..100, 100 = ready
  var g=F.ub&&F.ub.jj||[],c=100;
  for(var i=0;i<g.length;i++)for(var A=g[i].actions,j=0;j<A.length;j++)for(var L=A[j].actions,k=0;k<L.length;k++){
    var n=L[k];if(n.model===m&&n.action&&__SF2.superMods.indexOf(n.action.name)>=0)c=Math.min(c,n.bg>0?n.Qv/n.bg*100:0)}
  return c}
function __sf2SuperButton(t,F,touch){
  var s=t.Sp,m=F?__sf2SuperModel(F):null,show=__sf2HasSuper(m),x,y;
  s.node.R(show);t.SpT.R(show);if(!show)return;
  if(touch){x=-250;y=140}                      // left of the kick button
  else{x=(t.Kg.active?-117:-12)-224;y=(t.ii.active||t.Kg.active?77:-25)-90}   // left of the ranged button
  s.C(x);s.D(y);
  // the letter: on the button (touch: "S"), or under it like the other buttons' keys (keyboard: Q)
  var o=touch?[-14,-25]:[-8,70];t.SpT.V(touch?"S":"Q");t.SpT.C(x+o[0]);t.SpT.D(y+o[1]);
  s.G5(__sf2SuperCharge(F,m))}

// every content pack counts as downloaded (see the patch "content packs installed")
function __sf2InstallPacks(nc,cdn,ver){                       // what the game's DownloadPack action does
  __SF2.packs.forEach(function(n){var d=cdn.UQ(n);d&&!nc.o2(n)&&nc.QXa(d.name,d.url,ver,d.hi,d.hash)})}

// Hub and game fights use a profile of our own. A different save in this browser (an older one) is
// replaced once by the default profile (every item, max level). Only the save is deleted (SF2User,
// SF2Packs): the game's ka.reset() would also reset its settings (SF2Flags: sound, the intro skipped), and
// the intro would play again after every change of fight or difficulty.
function __sf2Profile(ka){
  if(!__SF2.profile)return;
  try{var k='__sf2_profile';if(localStorage.getItem(k)!==__SF2.profile){ka.MD(ka.oV).delete();ka.MD(ka.u7).delete();localStorage.setItem(k,__SF2.profile)}}
  catch(e){}}

var __sf2AudioNote=false;
function __sf2NoAudio(e){                       // browser cannot decode AAC (.m4a): play on without sound
  if(__sf2AudioNote)return;__sf2AudioNote=true;
  console.warn('[sf2] this browser cannot decode the game audio (AAC .m4a) - running without sound',e);
  var d=document.createElement('div');
  d.textContent='This browser cannot decode the game sounds (AAC) - the game runs silently. For sound use Chrome or Edge.';
  d.style.cssText='position:fixed;top:8px;left:50%;transform:translateX(-50%);z-index:99999;font:13px sans-serif;'+
    'color:#fff;background:rgba(120,60,0,.85);padding:6px 12px;border-radius:4px;pointer-events:none';
  (document.body||document.documentElement).appendChild(d);setTimeout(function(){d.remove()},12000)}

// ---------------------------------------------------------------- LAN lockstep
// Both browsers run the same deterministic simulation (fixed 1/60 s steps). A step runs only when both
// players' inputs for it are known; local input for step t is sent at step t-D.
// Both leave the VS screen together, re-seed the random generators from the server's seed, and compare
// a hash of the fight state every 30 steps.
var __sf2Lan=null;
(function(){
  if(!__SF2.lan)return;
  var BITS=['up','down','left','right','9','10','11','12','14'];
  var V=__SF2.keys.map(function(K){return BITS.map(function(b){return[].concat(K[b])[0]})});
  var PH={};                                   // any physical key of either set (+ K L O P) -> input bit
  __SF2.keys.concat([{9:[75],10:[76],11:[79],12:[80]}]).forEach(function(K){
    BITS.forEach(function(b,i){[].concat(K[b]||[]).forEach(function(k){PH[k]=i})})});
  var CB={1:1,2:9,3:8,4:10,5:2,6:6,7:4,8:5,9:16,10:32,11:64,12:128,14:256};   // touch control -> bits
  var L=__sf2Lan={side:-1,seed:0,peer:false,peerHidden:false,active:false,ended:false,fight:null,
    tick:0,sent:-1,D:3,rttMe:-1,rttThem:-1,inp:[{},{}],prev:[0,0],held:{},tc:{},tap:0,
    readyMe:false,readyThem:false,readySent:false,my:{},theirs:{},parts:{},ok:0,desyncAt:-1,
    lastStep:0,ping:-1,rs:1,ws:null,msg:'',maxThem:-1,lateAt:0,stalls:[],blocked:false,
    g:0,myGo:null,goThem:{},prepared:false};    // g = match number; every in-match message carries it

  function send(o){if(L.ws&&L.ws.readyState===1)L.ws.send(JSON.stringify(o))}
  function sendG(o){o.g=L.g;send(o)}
  function peerOn(on){
    L.peer=on;L.readySent=false;
    if(on){
      // the other one (re)joined with fresh match numbers. Mid-match: both start over. In the dojo or at the
      // VS screen: restart the numbering (and send my "go" again)
      if(L.active||L.ended||L.prepared){location.reload();return}
      L.goThem={};L.g=L.myGo?1:0;if(L.myGo){L.myGo.g=1;send(L.myGo)}}
    if(!on){L.readyThem=false;L.peerHidden=false;
      if(L.active){L.active=false;L.ended=true;L.msg='opponent left - a new match starts when they are back'}}}
  (function connect(){
    // the network server the menu was given (the two menus agreed on the match before: role lobby-*)
    var ws=L.ws=new WebSocket('ws://'+__SF2.relay+'/sf2-lan?role='+__SF2.role);
    ws.onmessage=function(e){var m=JSON.parse(e.data);
      switch(m.t){
        case 'go':L.goThem[m.g]=m;break;
        case 'i':m.g===L.g&&(L.inp[1-L.side][m.f]=m.m,L.maxThem=Math.max(L.maxThem,m.f));break;
        case 'late':m.g===L.g&&L.active&&!(__SF2.delay>0)&&performance.now()-L.lateAt>3000&&L.D<Math.min(10,L.D0+3)&&(L.D++,L.lateAt=performance.now());break;   // at most 3 frames more
        case 'h':if(m.g===L.g){L.theirs[m.f]=m.h;cmp(m.f)}break;
        case 'dbg':if(m.g===L.g)__sf2Report('desync','frame '+m.f+' differs (mine / the other side\'s): '+detail(m.f,m.p)+
          ' - the other side runs '+m.ua);break;
        case 'ready':if(m.g===L.g){L.readyThem=true;L.rttThem=m.rtt}break;
        case 'vis':L.peerHidden=!!m.hidden;break;
        case 'p':send({t:'q',ts:m.ts});break;
        case 'q':L.ping=Math.round(performance.now()-m.ts);break;
        case 'hello':L.side=m.side;L.seed=m.seed;peerOn(!!m.peer);break;
        case 'peer':peerOn(!!m.on);break;
        case 'full':L.msg='room full (two players already connected)';break;
      }};
    ws.onclose=function(){L.peer=false;L.msg='lost the connection to the server'};
  })();
  setInterval(function(){if(L.peer)send({t:'p',ts:performance.now()})},1000);
  document.addEventListener('visibilitychange',function(){send({t:'vis',hidden:document.hidden})});

  var box=null;
  setInterval(function(){
    if(!box&&document.body){box=document.createElement('div');
      box.style.cssText='position:fixed;bottom:4px;left:50%;transform:translateX(-50%);z-index:99999;font:12px monospace;'+
        'color:#fff;padding:3px 10px;border-radius:4px;pointer-events:none;white-space:nowrap';
      document.body.appendChild(box)}
    if(!box)return;
    var s='LAN · you: '+(L.side<0?'?':L.side?'RIGHT':'LEFT')+' · '+(L.peer?'opponent connected':'waiting for the opponent');
    if(L.ping>=0&&L.peer)s+=' · ping '+L.ping+'ms';
    if(L.peerHidden)s+=' · the opponent\'s tab is in the background, the game is paused';
    else if(L.myGo&&!L.goThem[L.g])s+=' · the opponent has not pressed fight yet';
    else if(L.readyMe&&!L.active&&!L.ended&&!L.readyThem)s+=' · waiting for the opponent to load';
    else if(L.active&&performance.now()-L.lastStep>500)s+=' · waiting for the opponent\'s input';
    if(L.active)s+=' · delay '+L.D+' frames · sync ✓'+L.ok;
    if(L.desyncAt>=0)s+=' · OUT OF SYNC (frame '+L.desyncAt+')';
    if(L.msg)s+=' · '+L.msg;
    box.textContent=s;box.style.background=L.desyncAt>=0?'rgba(160,0,0,.85)':'rgba(0,0,0,.65)';
  },250);

  // The simulation's random numbers must come only from the simulation. While a lockstep step runs (L.sw)
  // the game's generator Da.vg, its counter J.seed and Math.random (patched to L.rnd) are the match's own.
  // Outside the steps (sounds, effects, popups, timers, which differ per device) they are the normal ones,
  // so nothing there can change the match's dice (a crit roll once came out differently).
  L.rnd=function(){if(!L.sw)return Math.random();var t=L.rs=(L.rs+0x6D2B79F5)|0;t=Math.imul(t^t>>>15,t|1);t^=t+Math.imul(t^t>>>7,t|61);
    return((t^t>>>14)>>>0)/4294967296};
  L.reseed=function(J,Da,k){var s=((L.seed|0)+k*7919)%2147483646+1;J.seed=s;Da.bU(s);L.rs=s;return s};
  var fb=new Float64Array(1),ib=new Int32Array(fb.buffer);
  function mix(h,x){fb[0]=+x||0;h=Math.imul(h^ib[0],16777619);return Math.imul(h^ib[1],16777619)}
  function hash(){var h=-2128831035;try{[L.fight.zb,L.fight.pb].forEach(function(w){
      var ns=w.pa.Va.all;for(var i=0;i<ns.length;i++){var p=ns[i].ma;h=mix(mix(mix(h,p.x),p.y),p.z)}
      [w.Bo,w.Yy,w.Ne,w.By,w.fh,w.RV].forEach(function(x){h=mix(h,x)})})}catch(e){h=-1}
    return h}
  var PART=['nodes','Bo','Yy','Ne','By','fh','RV'];   // what the hash is made of, per fighter (left, right)
  function parts(){try{return[L.fight.zb,L.fight.pb].map(function(w){
      var h=-2128831035,ns=w.pa.Va.all;for(var i=0;i<ns.length;i++){var p=ns[i].ma;h=mix(mix(mix(h,p.x),p.y),p.z)}
      var m=w.ea&&w.ea.Ua,r=ns[0]&&ns[0].ma;            // + not hashed: the move it is in, where it stands
      return[h>>>0,w.Bo,w.Yy,w.Ne,w.By,w.fh,w.RV,m&&m.name,r&&Math.round(r.x)+','+Math.round(r.y)]})
      .concat([[L.rs|0,L.sim&&L.sim.seed]])}catch(e){return null}}    // + the match's dice: L.rnd's state, J's counter
  function detail(f,theirs){                     // the other side's parts of frame f against mine -> what differs
    var mine=L.parts[f],out=[];if(!mine||!theirs)return'(no parts kept for frame '+f+')';
    for(var s=0;s<2;s++)for(var i=0;i<PART.length;i++)if(mine[s][i]!==theirs[s][i])
      out.push((s?'RIGHT':'LEFT')+'.'+PART[i]+' '+mine[s][i]+' / '+theirs[s][i]);
    for(s=0;s<2;s++)out.push((s?'RIGHT':'LEFT')+' move '+mine[s][7]+' / '+theirs[s][7]+' at '+mine[s][8]+' / '+theirs[s][8]);
    mine[2]&&theirs[2]&&out.push('dice '+mine[2].join(':')+' / '+theirs[2].join(':'));
    return out.join(', ')}
  function cmp(f){var a=L.my[f],b=L.theirs[f];if(a===undefined||b===undefined)return;
    if(a!==b){if(L.desyncAt<0){L.desyncAt=f;console.warn('[sf2-lan] desync at frame',f);
      var F=L.fight,r=F&&F.round&&F.round.round;   // -> server.py's window: where and on what it broke
      __sf2Report('desync','OUT OF SYNC at frame '+f+' (round '+r+', '+L.ok+' checks fine before, input delay '+L.D+
        ' frames, ping '+L.ping+' ms, side '+(L.side?'RIGHT':'LEFT')+') on '+navigator.userAgent);
      sendG({t:'dbg',f:f,p:L.parts[f]||null,ua:navigator.userAgent})}}else L.ok++;
    delete L.my[f];delete L.theirs[f]}

  // input: keys and touch never reach the fighters directly, only through the lockstep
  L.key=function(b,down,ev){
    if(!__sf2Hub.inMatch)return false;         // the dojo: keys work normally
    ev&&ev.preventDefault&&ev.preventDefault();
    var bit=PH[b];
    if(L.active&&bit!==undefined){if(down){if(!ev.repeat){L.held[bit]=1;L.tap|=1<<bit}}else delete L.held[bit]}
    return true};
  L.touch=function(ev,down){
    if(!__sf2Hub.inMatch||!ev||ev.aga()!==0)return false;
    var c=ev.control;
    if(L.active){if(down){L.tc[c]=1;L.tap|=CB[c]|0}else delete L.tc[c]}
    return true};
  function sample(){var m=L.tap;for(var k in L.held)m|=1<<k;for(var c in L.tc)m|=CB[c]|0;L.tap=0;return m}
  function apply(s,m){var d=m^L.prev[s];if(!__sf2Kb||!d)return;
    for(var i=0;i<V[s].length;i++)if(d>>i&1)(m>>i&1)?__sf2Kb.state.wl(V[s][i]):__sf2Kb.state.release(V[s][i]);
    L.prev[s]=m}

  // a new match: my equipment, the perks chosen on my dojo's perk screen (my save's, which the other device
  // does not have: a story fight's player is built from the save) + a random number go to the other PC
  L.go=function(zc){
    L.g++;zc&&__SF2.custom&&__sf2SaveOwn(zc);
    L.myGo={t:'go',g:L.g,items:zc?__sf2Equip(zc):null,perks:zc?zc.cF.filter(Boolean).map(__sf2PerkKey):null,
      nonce:(Math.random()*4294967296)>>>0};send(L.myGo)};
  // both lists are here -> [left items, right items]; the seed comes from both random numbers
  L.items=function(){
    var them=L.goThem[L.g];if(!L.myGo||!them)return null;
    var a=L.side?them:L.myGo,b=L.side?L.myGo:them;
    if(__sf2Hub.on)L.seed=(Math.imul(a.nonce,2654435761)^b.nonce)>>>0;
    L.prepared=true;L.perks=[a.perks,b.perks];return[a.items,b.items]};
  // back in the dojo: forget the match
  L.reset=function(){
    L.active=L.ended=L.prepared=L.readyMe=L.readyThem=L.readySent=false;L.myGo=null;delete L.goThem[L.g];
    L.inp=[{},{}];L.my={};L.theirs={};L.parts={};L.ok=0;L.desyncAt=-1;L.tick=0;L.prev=[0,0];L.held={};L.tc={};L.tap=0;
    L.fight=null;L.msg=''};

  // called by the patched game
  L.ready=function(F){                          // VS screen may end?  (scene state 3)
    if(!__sf2IsMatch(F))return true;
    if(!F.loaded)return false;
    L.readyMe=true;
    if(L.peer&&!L.readySent){L.readySent=true;L.rttMe=L.ping;sendG({t:'ready',rtt:L.rttMe})}
    if(!(L.peer&&L.readyThem))return false;
    // input delay: both sides know both round trips, so both pick the same value. A key press must reach
    // the other device (half a round trip plus a few ms) before the frame that uses it is simulated
    var r=Math.max(L.rttMe,L.rttThem);
    L.D=L.D0=__SF2.delay>0?__SF2.delay:L.rttMe<0||L.rttThem<0?3:Math.min(8,Math.max(1,Math.ceil((r/2+4)/16.67)));
    return true};
  L.start=function(F,J,Da){                     // first fight update (scene state 5)
    if(L.active||L.ended||!__sf2Hub.inMatch||!__sf2IsMatch(F))return;   // not while leaving the match
    L.fight=F;L.tick=0;L.prev=[0,0];L.held={};L.tc={};L.tap=0;L.lastStep=performance.now();
    for(var t=0;t<L.D;t++){L.inp[0][t]=0;L.inp[1][t]=0}
    L.maxThem=L.D-1;L.stalls=[];L.blocked=false;
    L.sent=L.D-1;var s=L.reseed(J,Da,2);
    L.J=J;L.Da=Da;L.sim={vg:new Da.vg.constructor(s),seed:s};L.sw=false;
    __sf2Kb&&__sf2Kb.state.reset();
    L.active=true};
  // The result is decided (the same frame on both devices), so the lockstep ends here. The end animation
  // and result screen wait for GameInterface's gameOver/gameComplete promise, which resolves at a different
  // moment on each device; nobody has to wait for the other to leave the result screen.
  L.over=function(F){if(L.active&&F===L.fight){L.active=false;L.ended=true;
    console.info('[sf2-lan] match over at frame',L.tick,'- checks ok:',L.ok,L.desyncAt<0?'':'DESYNC at '+L.desyncAt)}};
  L.canStep=function(){                         // may the engine run its next fixed step?
    if(!L.active)return true;
    while(L.sent<L.tick+L.D){L.sent++;var m=sample();L.inp[L.side][L.sent]=m;sendG({t:'i',f:L.sent,m:m})}
    var ok=L.inp[1-L.side][L.tick]!==undefined,now=performance.now();
    if(!ok&&!L.blocked){                         // the other's input is late: 3 times in 5 s -> it should send
      L.stalls=L.stalls.filter(function(x){return now-x<5000});L.stalls.push(now);   // earlier (a frame more of delay)
      L.stalls.length>=3&&(L.stalls=[],sendG({t:'late'}))}
    L.blocked=!ok;return ok};
  // Smooth pace: the clock runs a little slower while the other's inputs only just arrive (this device is
  // ahead), a little faster when they are far ahead (behind), so the two meet without a stop. A stop does not
  // pile up time either: no burst of steps after it (patch "LAN pacing")
  L.pace=function(){if(!L.active)return 1;var s=L.maxThem-L.tick;return s<1?.92:s>L.D+3?1.08:1};
  L.pre=function(){                             // right before step L.tick
    if(!L.active)return;var t=L.tick;
    if(t%10===0){var h=hash();L.my[t]=h;L.parts[t]=parts();delete L.parts[t-600];sendG({t:'h',f:t,h:h});cmp(t)}
    for(var s=0;s<2;s++){apply(s,L.inp[s][t]|0);delete L.inp[s][t-120]}
    L.tick++;L.lastStep=performance.now();
    L.ui={vg:L.Da.vg,seed:L.J.seed};L.Da.vg=L.sim.vg;L.J.seed=L.sim.seed;L.sw=true};
  L.post=function(){                            // right after it (also the step in which the match ended)
    if(!L.sw)return;
    L.sim.seed=L.J.seed;L.Da.vg=L.ui.vg;L.J.seed=L.ui.seed;L.sw=false};
})();
"""

def _audio_decode(m):
    return ('(Uc.$i.decodeAudioData(a,' + m.group(1) +
            'function(c){__sf2NoAudio(c);b.complete(new Uc(Uc.$i.createBuffer(1,1,44100)))})'
            '||{catch:function(){}}).catch(function(){});return b.jf}static split')


# The game saves (SF2User, SF2Packs) through window.GameInterface.storage: a native store in the
# Microsoft Store app, but only memory in a browser or the APK, so a reload lost the save. They go to
# this browser's localStorage instead (sf2save:*).
SAVE_PATCH = ("save in this browser",
              r'class Gk extends Sg\{constructor\(a\)\{super\(a\)\}load\(\)\{.*?\}\}Gk\.g="8"',
              'class Gk extends Sg{constructor(a){super(a)}'
              'load(){let a=null;try{a=window.localStorage.getItem("sf2save:"+this.name)}catch(b){}return qc.po(a)}'
              'save(a){try{window.localStorage.setItem("sf2save:"+this.name,a)}catch(b){}return qc.po(!0)}'
              'delete(){try{window.localStorage.removeItem("sf2save:"+this.name)}catch(b){}return qc.po(!0)}}Gk.g="8"')
# (name, regex on the minified sf2.js, replacement text or function). Each regex must match exactly once.
JS_PATCHES = [
    SAVE_PATCH,
    ("expose internals",
     r'Ka\.SF2=Ka\.SF2\|\|\{\};',
     'Ka.SF2=Ka.SF2||{};window.__sf2API={get p(){return p},get v(){return v},get ya(){return ya},'
     'get fight(){return ca.Ka()},get uy(){return uy},get na(){return na},get ei(){return ei},'
     'get J(){return J},get Da(){return Da},get Yc(){return Yc},get za(){return za},get jd(){return jd}};'),
    # ---- scenes
    ("scene changes",
     r'xp\(a,b,c,d\)\{d==null&&\(d=!0\);if\(!ae\.YZa\(\)\|\|ya\.fZa\)return!1;',
     'xp(a,b,c,d){if(__sf2Nav(this,a))return!0;d==null&&(d=!0);if(!ae.YZa()||ya.fZa)return!1;'),
    # Content packs: the web build "downloads" a pack by writing it into the SF2Packs registry (Un.S ->
    # Nc.QXa) and reloading; all files are local. The registry decides which moves exist (Fa.tdb, PackName)
    # and whether the effect atlases load (ifa: pack ZONE_1). Every pack of res/packs.xml is written the same
    # way right after boot reads the registry, so nothing is missing and no download dialog shows.
    ("content packs installed",
     r'Nc\.F\(\)\.ZS\(\);id\.F\(\)\.Dyb\(\)',
     'Nc.F().ZS();id.F().Dyb();__sf2InstallPacks(Nc.F(),id.F().Am,yg.version.toString())'),
    # the game's text language comes from the portal layer = the device language; "language" fixes it
    ("language",
     r'static T6a\(\)\{return window\.GameInterface\.getCurrentLanguage\(\)\}',
     'static T6a(){return __SF2.language||window.GameInterface.getCurrentLanguage()}'),
    ("hub profile",
     r'\(this\.lE=!ka\.Je\(\)\)&&ka\.init\(\);',
     # lE = "no save yet, first launch": the save is created (ka.init) and the dojo loads only the
     # tutorial's small resource set, without shop and inventory art, so the item list read null.
     # The hub profile is not a tutorial player, so the full dojo loads from the start
     '__sf2Profile(ka);(this.lE=!ka.Je())&&ka.init();__SF2.profile&&(this.lE=!1);'),
    ("hub equipment into the match",
     r'case 1:if\(!this\.lz\)break;this\.Ng=\s*v\.tya\(',
     'case 1:if(!this.lz||!__sf2Prepare(this,p))break;this.Ng=v.tya('),
    # who fights: see __sf2Sides (custom match: the left fighter from the XML; game fight: who controls whom)
    ("sides",
     r'b\.G=d;c\.G=e;',
     'e=__sf2Sides(a,d,e,function(n){return P.Hfa(n)});d=e[0];e=e[1];b.G=d;c.G=e;'),
    # LAN: the opponents are picked here (random warrior groups use the game's generator Da.vg), so both
    # browsers need the same generator state
    ("LAN seed at opponent pick",
     r'static XJa\(a,\s*b,\s*c\)\{',
     'static XJa(a,b,c){__sf2Lan&&__sf2Ours(a)&&__sf2Lan.reseed(J,Da,3);'),
    # ... so the fight's warrior list (kt) holds the left fighter too, which the game counts as an opponent.
    # Two "opponents" and more than one round make a gauntlet (GR): winning a round brings the next
    # opponent, which does not exist (undefined.position, then a cleared fighter .kd)
    ("left fighter is no opponent",
     r'GR\(\)\{return this\.kt\.length>\s*1\?this\.JT>1:!1\}',
     'GR(){return this.kt.length-(__sf2Custom(this)?1:0)>1?this.JT>1:!1}'),
    # the same count gives the number of the last round (round banner)
    ("left fighter is no opponent (rounds)",
     r'p7a\(\)\{let a=0,b=0,c=this\.kt;for\(;b<c\.length;\)a\+=c\[b\+\+\]\.number;return a\}',
     'p7a(){let a=0,b=0,c=this.kt;for(;b<c.length;)a+=c[b++].number;return a-(__sf2Custom(this)?c[0].number:0)}'),
    # The fight scene preloads an arena's files from a table (Ef.LAa) that lacks the dojo (a fight in the
    # dojo threw 20) and arenas added to the file list (Android's). Their entries are their files' numbers
    # in the game's file list (G.Bq)
    ("locations missing in the fight's preload table",
     r'static LAa\(a\)\{switch\(a\.location\)\{',
     lambda m, src: 'static LAa(a){switch(a.location){' + "".join(
         f'case "{n}":return{json.dumps(location_files(src, n))};' for n in missing_locations(src))),
    # the fight scene's music also comes from a table (va.N0: name -> file number); music added to the
    # file list (audio/<name>_music) gets an entry
    ("music missing in the music table",
     r'(=\s*\d+;)(va\.hHa=b\})',
     lambda m, src: m.group(1) + "".join(f'b.v.{n}={i};' for n, i in missing_music(src)) + m.group(2)),
    # ... and the dojo scene deletes the dojo arena's files when it is left (E.nD). Going from the dojo into
    # a fight in the dojo, that pulled the files from under the fight (undefined texture 'width'). Now the
    # dojo keeps them; the fight scene releases them when it ends
    ("dojo location kept for a fight in the dojo",
     r'a instanceof Vf\|\|\(\$a\.B\(\),E\.nD\(([0-9,]+)\)',
     lambda m: 'a instanceof Vf||($a.B(),__sf2Hub.inMatch&&__sf2Hub.loc=="dojo"||E.nD(' + m.group(1) + ')'),
    # ... and the fight scene's screen area (na.Wq: the result dialog with OK / STORE) is the arena's height
    # projected onto the screen. Every fight arena is 512 high, the dojo 560, which pushed the result
    # buttons off screen. The area uses 512 at most
    ("dojo fight result dialog",
     r'c=-e/2;f=M\.rect;d=b\.TA;(.*?)e/=2;f=M\.rect;d=b\.TA;',
     lambda m: 'c=-Math.min(e,512)/2;f=M.rect;d=b.TA;' + m.group(1) + 'e=Math.min(e,512)/2;f=M.rect;d=b.TA;'),
    # A move's SetEndStage action (GatekeeperLoss: a ragdoll without animation, so no animation end to end
    # the round's end stage) did nothing: the loser never ended its stage and the next round never started.
    # It now sets the fighter's stage-ended flag, as the end of an EndsStage move does (FIa)
    # A move's lock <Item Player="Enemy" .../> (TitanThrowForwardV: the victim's half of Titan's throw, unlocked
    # when the enemy holds the giant sword) was checked against the fighter's own items: Titan got the
    # victim's move and his victims did not, so his throw grabbed nothing. Player="Enemy" now checks the
    # enemy's items (jb; none yet: locked)
    # Two players: the game gives a round to the opponent (the right side) on a time-out (no rule judging)
    # and on a knockout where both are down (health equal). Two players are judged like the game's own PvP
    # fights (ty 6): more health wins, equal = nobody gets the round
    ("two-player round result",
     r'this\.v4a\(this\.Qfa\(!0\),this\.Qfa\(!1\),this\.ty\)\)\}',
     'this.v4a(this.Qfa(!0),this.Qfa(!1),this.ty=__sf2Judge(this)))}'),
    # The camera keeps the player (the left fighter) in view when the two are far apart, and the pulsing light
    # is under it. On the second network device the player is the right fighter: the two are swapped there
    # (the camera only places the picture; the simulation does not read it)
    ("camera and light follow this device's fighter",
     r'U3a\(\)\{let a=\s?this\.Nu\.ma,b=new H\(this\.D\$\.ma\.x,this\.D\$\.ma\.y,0,1\),c=new H\(this\.E\$\.ma\.x,this\.E\$\.ma\.y,0,1\);',
     'U3a(){let a=this.Nu.ma,s=__sf2Lan&&__sf2Lan.side==1,b=s?this.E$:this.D$,c=s?this.D$:this.E$;b=new H(b.ma.x,b.ma.y,0,1);c=new H(c.ma.x,c.ma.y,0,1);'),
    # A text key "SF2TEXT_..." is its own text: the own fighter's name (FirstName is a text key; a name
    # that is no key shows %%ERROR%% on the VS screen)
    ("literal texts",
     r'if\(!X\.Xa\(this\.iV,b\)\)return rc\.L7;',
     'if(!X.Xa(this.iV,b))return b.indexOf("SF2TEXT_")==0?b.slice(8):rc.L7;'),
    ("item lock on the enemy",
     r'class ym extends Ha\{constructor\(a\)\{super\(13\);',
     'class ym extends Ha{constructor(a){super(13);this.__sf2Enemy=a.attributes.get("Player")=="Enemy";'),
    ("item lock on the enemy (check)",
     r'\}je\(a\)\{let b=0;for\(a=a\.items;b<a\.length;\)\{var c=a\[b\];\+\+b;var d=void 0;d=this\.QR;',
     '}je(a){let b=0;for(a=this.__sf2Enemy?a.Cr&&a.Cr.jb?a.Cr.jb.parameters.qp():[]:a.items;b<a.length;)'
     '{var c=a[b];++b;var d=void 0;d=this.QR;'),
    ("SetEndStage action",
     r'class gm extends fb\{constructor\(a\)\{super\(15\);this\.parse\(a\)\}Zh\(\)\{\}',
     'class gm extends fb{constructor(a){super(15);this.parse(a)}Zh(a){a.DR=!0}'),
    # an effect's StartRotation is in degrees (Saturn's second shot 5, the musket 90, Arcane's dash 180; the
    # particles turn theirs by 0.01745), but the effect used it as radians: Saturn's shot flew upwards
    ("effect rotation in degrees",
     r'rotate\(\)\{let a=this\.container\.node\.Kb;var b=this\.effect\.pma\*this\.model\.kd\(\);',
     'rotate(){let a=this.container.node.Kb;var b=this.effect.pma*.0174532925199432*this.model.kd();'),
    # A fighter's rating reads a perk's Aspect. The compiled code lost Jma's out value and parsed nothing
    # (ky(null) throws 18), so a fight with a rated perk that has a set Aspect (enchantments like Overheat
    # defense, the underworld's hard mode) never started. Now the Aspect comes from the perk's set values
    # (qC), parsed the way the perk itself does (cd)
    ("perk Aspect in the rating",
     r'(hCa\(a,b\)\{.*?)C=new Ia;r\.Jma\(zc\.AX\)&&\(A=ky\(C\.G\)\)(.*?)C=new Ia;r\.Jma\(zc\.AX\)&&\(A=ky\(C\.G\)\)',
     lambda m: m.group(1) + 'C=cd(r.qC.get(zc.AX));C!=null&&(A=C)' + m.group(2) + 'C=cd(r.qC.get(zc.AX));C!=null&&(A=C)'),
    # a fight without Music (the punching bag in the dojo) had no music file to preload (null file name).
    # The game's own default for no music is "menu" (eb.fT)
    ("fight without music",
     r'd=b\.attributes\.get\("Music"\);a\.Ep=d!=null\?d:e;',
     'd=b.attributes.get("Music");a.Ep=d!=null?d:e!=null?e:"menu";'),
    # A FightNone fight (DUMMY: the dojo's punching bag and disciple) runs inside the dojo scene in the game,
    # which has no health or round panels. In the fight scene the panels exist but are set up per round,
    # and FightNone has no rounds; the rules then refresh a panel that was never set up
    ("FightNone in the fight scene",
     r'YKa\(\)\{this\.\$H\.A3a\(\)\}',
     'YKa(){this.$H&&this.$H.A3a()}'),
    # every XML file the game reads (Ja.pi: a loose file or one out of xml2.dat) passes __sf2Xml
    ("XML files",
     r'static pi\(a\)\{let b=G\.ig\(a\);a=G\.Zk\(a\);return a!=\s*null\?Sb\.parse\(a\):Sb\.ija\(Ja\.gea\(Ja\.xml,b\)\)\}',
     'static pi(a){let b=G.ig(a);a=G.Zk(a);return a!=null?Sb.parse(__sf2Xml(b,a)):Sb.ija(__sf2Xml(b,Ja.gea(Ja.xml,b)))}'),
    # two players in lockstep: no pause button (it would stop one browser only); the portal's feature list
    ("no pause in lockstep",
     r'static hasFeature\(a\)\{return window\.GameInterface\.hasFeature\(a\)\}',
     'static hasFeature(a){return a=="pause"&&__SF2.lan?!1:window.GameInterface.hasFeature(a)}'),
    # an uncaught error must not freeze the game (see __sf2Error)
    ("errors keep the game running",
     r'EA\(\)\{this\.Nc\.stop\(\);window\.removeEventListener\("error",w\(this,this\.EA\)\);'
     r'window\.removeEventListener\("unhandledrejection",w\(this,this\.EA\)\);ua\.console\.log\("\\ud83d\\udc80"\)\}',
     'EA(a){__sf2Error(a)}'),
    # ---- keyboard: two key sets, one direction per frame per player (the game allows one in total)
    ("keyboard key sets",
     r'this\.keyboard\.He\(14,0,a\.get\(14\)\)\}',
     'this.keyboard.He(14,0,a.get(14));__sf2BindKeys(this.keyboard)}'),
    ("keyboard direction reset",
     r'kca\(\)\{this\.bE=!1;let a=0',
     'kca(){this.bE=!1;this.bEi={};let a=0'),
    ("keyboard direction per player",
     r'jca\(a\)\{a\.mDa\(\)&&\(a\.Eb\(\)\|\|a\.fj\(\)\)\?this\.bE\|\|\(this\.bE=!0,this\.yr\.Z\(a\)\)',
     'jca(a){a.mDa()&&(a.Eb()||a.fj())?((this.bEi||(this.bEi={}))[a.index]||(this.bEi[a.index]=!0,this.yr.Z(a)))'),
    ("fighter bindings",
     r'Oc\.T\$a\(\)&&\(Oc\.ida=\s*0\)',
     '__sf2FighterBindings(Oc,yy,this)'),
    ("player light hidden while invisible",
     r'this\.dzb\(this\.EB,',
     '__sf2Light(this.EB)&&this.dzb(this.EB,'),
    # ModInvisibility hides the fighter's model (go.setActive): not on its own player's screen (__sf2Cloak)
    ("own fighter seen while invisible",
     r'bqb\(a,b\)\{this\.Ta\.ka\.Yf\.th\.O5\(a\.pa,b\)\}',
     'bqb(a,b){__sf2Cloak(a,b)||this.Ta.ka.Yf.th.O5(a.pa,b)}'),
    # ... and a fighter part's colour (La: alpha 1 always; mesh Jk, capsules Hk) keeps that ghost alpha
    ("fighter mesh alpha",
     r'La\(a\)\{this\.e\.color=(\s*)Oa\.dd\(a\)\}(On\(\)\{super\.On\(\);)',
     lambda m: 'La(a){this.e.color=' + m.group(1) + 'Oa.dd(a);this.__sf2A!=null&&(this.e.color.w=this.__sf2A)}' + m.group(2)),
    ("fighter capsule alpha",
     r'La\(a\)\{this\.e\.color=(\s*)Oa\.dd\(a\)\}(On\(\)\{this\.e=new )',
     lambda m: 'La(a){this.e.color=' + m.group(1) + 'Oa.dd(a);this.__sf2A!=null&&(this.e.color.w=this.__sf2A)}' + m.group(2)),
    # the AI's view of an invisible opponent: its last seen place (__sf2SeenX / __sf2SeenAt)
    ("AI tactics: invisible opponent last seen",
     r'a=Math\.abs\(this\.xN\.ma\.x-this\.MO\.ma\.x\)',
     'a=Math.abs(__sf2SeenX(this.zo.kf(1),this.zo.kf(this.VB),this.xN)-__sf2SeenX(this.zo.kf(1),this.zo.kf(this.Faa),this.MO))'),
    ("AI move conditions: invisible opponent last seen",
     r'At\(a\)\{var b=this\.xfa\(a\),c=new H\(0,0,0,1\);',
     'At(a){return __sf2SeenAt(this,a,this.__sf2At(a))}__sf2At(a){var b=this.xfa(a),c=new H(0,0,0,1);'),
    ("AI move tactics flag",
     r'xw\(a,b,c\)\{b=b!=null\?b:this\.xa\.sb;c!=null&&\(a\.h_=c,c\.sb=a\);for\(c=0;c<b\.length;\)if\(!b\[c\+\+\]\.je\(a\)\)return!1;return!0\}',
     'xw(a,b,c){b=b!=null?b:this.xa.sb;c!=null&&(a.h_=c,c.sb=a);var t=__sf2TacOn;__sf2TacOn=b===this.xa.et;'
     'try{for(c=0;c<b.length;)if(!b[c++].je(a))return!1;return!0}finally{__sf2TacOn=t}}'),
    # ... until the invisible one hits (__sf2Felt)
    ("AI feels where an invisible attacker is",
     r'(static MAa\(a,b,c,d,e,f,g,h\)\{)',
     lambda m: m.group(1) + '__sf2Felt(b);'),
    # "Moves variables" (Android's data; the web engine has none): SetMovesVariable keeps a number (Karcer
    # set: where the orb of hunger hangs, the enemy's X at the 4th hit; Rakshasa's curse zone),
    # ?MovesVariable[] reads it, and an effect's Position ShiftX may name it (the orb is drawn there).
    # Kept where SetModVariable keeps its own (the fighter's Gc.j4), read the way ?Variable[] is (Eib)
    ("SetMovesVariable action",
     r'case "SetModVariable":g=new hq;break;',
     'case "SetModVariable":g=new hq;break;case "SetMovesVariable":f.set("Name",f.attributes.get("Variable"));g=new hq;break;'),
    ("?MovesVariable[]",
     r'case "Variable":this\.Eib\(d,b,c\);break;',
     'case "Variable":case "MovesVariable":this.Eib(d,b,c);break;'),
    ("position from a moves variable",
     r'this\.wx=a!=null\?u\.H\(a\.attributes\.get\("ShiftX"\),0\):0;(this\.xx=a!=null\?u\.H\(a\.attributes\.get\("ShiftY"\),0\):0\})',
     lambda m: 'this.wx=a!=null?u.H(a.attributes.get("ShiftX"),0):0;var v=a!=null?a.attributes.get("ShiftX"):null;'
               'this.__sf2Vx=v!=null&&v!=""&&isNaN(+v)?v:null;' + m.group(1)),
    ("position from a moves variable (read)",
     r'At\(a\)\{return __sf2SeenAt\(',
     'At(a){this.__sf2Vx!=null&&(this.wx=+(a.j4&&a.j4.get(this.__sf2Vx))||0);return __sf2SeenAt('),
    ("hide touch controls",
     r'Fa\(\)\{if\(this\.isVisible&&this\.enabled\)\{var a=this\.xh\.node;',
     'Fa(){if(this.isVisible&&this.enabled&&!__SF2_HIDE_TOUCH){var a=this.xh.node;'),
    # ---- Super button (see __sf2SuperButton): a fifth button with the magic button's look, charge ring
    # and a letter (the game's text); a gamepad gets Super on the right shoulder button
    ("touch Super button",
     r'this\.Kg\.zL\(100,1\);',
     'this.Kg.zL(100,1);this.Sp=new ng(y.yRa);this.Sp.km=y.xRa;this.Sp.ge=14;this.Sp.pva();'
     'this.node.appendChild(this.Sp.node);this.Cd.push(this.Sp);this.SpT=new ea(E.Ma(),this.node);'
     'this.SpT.Ea(pc.CB,70);this.SpT.ua(70);this.SpT.Ga(64);this.SpT.La(Oa.dd(16776391));this.Sp.node.R(!1);'),
    ("touch Super button update",
     r'update\(\)\{for\(var a=0,b=this\.Cd;a<b\.length;\)b\[a\+\+\]\.aa\(J\.I\.vk\.Jm\);',
     'update(){__sf2SuperButton(this,ca.Ka(),J.I.om);for(var a=0,b=this.Cd;a<b.length;)b[a++].aa(J.I.vk.Jm);'),
    ("gamepad Super",
     r'this\.gamepad\.He\(12,2\)\}',
     'this.gamepad.He(12,2);this.gamepad.He(14,5)}'),
    # ---- audio
    # the audio context starts on pointerup only (autoplay rule); a key press is a user gesture too
    ("audio unlock by key",
     r'La\.n1\(\)\|\|window\.addEventListener\("pointerup",La\.PK\)',
     'La.n1()||window.addEventListener("pointerup",La.PK);La.kd||(La.kd=1,window.addEventListener("keydown",function(){'
     'La.context!=null&&La.context.state!="running"&&La.context.resume().catch(function(){})},!0))'),
    # Browsers without AAC (many Chromium builds) fail to decode the .m4a files. decodeAudioData then rejects
    # a promise the game never catches, and the game's "unhandledrejection" handler stops the main loop
    # (stuck at "Loading"). Decode errors now become silent sounds and the promise is caught.
    ("audio decode error",
     r'Uc\.\$i\.decodeAudioData\(a,(.*?)function\(c\)\{b\.error\(c\)\}\);return b\.jf\}static split',
     _audio_decode),
    # ---- LAN lockstep
    ("LAN keyboard device",
     r'this\.Ws\(\);this\.aKa=\[\];',
     '__sf2Kb=this;this.Ws();this.aKa=[];'),
    ("LAN keydown",
     r'this\.aKa\[b\]&&a\.preventDefault\(\);a\.repeat\|\|this\.state\.wl\(b\);',
     'if(__sf2Lan&&__sf2Lan.key(b,!0,a))return;this.aKa[b]&&a.preventDefault();a.repeat||this.state.wl(b);'),
    ("LAN keyup",
     r'this\.state\.release\(b\);this\.event=a;this\.dIa',
     'if(__sf2Lan&&__sf2Lan.key(b,!1,a))return;this.state.release(b);this.event=a;this.dIa'),
    ("LAN touch press",
     r'xS\(a\)\{if\(a\.control!=0&&this\.dFa\(a\.control\)\)',
     'xS(a){if(__sf2Lan&&__sf2Lan.touch(a,!0))return;if(a.control!=0&&this.dFa(a.control))'),
    ("LAN touch release",
     r'H3\(a\)\{if\(this\.dFa\(a\.control\)\)',
     'H3(a){if(__sf2Lan&&__sf2Lan.touch(a,!1))return;if(this.dFa(a.control))'),
    ("LAN pacing",
     r'b\.Uy\+=a\*b\.XL;b\.Uy>\.25&&\(b\.Uy=\.25\);',
     'b.Uy+=a*b.XL*(__sf2Lan?__sf2Lan.pace():1);b.Uy>.25&&(b.Uy=.25);__sf2Lan&&__sf2Lan.blocked&&b.Uy>b.Jm&&(b.Uy=b.Jm);'),
    # fixed-step loop: a step runs only when both players' inputs for it are known
    ("LAN step gate",
     r'for\(a=!1;b\.Uy>=b\.Jm;\)b\.lyb\+\+,c\(b\.Jm\)',
     'for(a=!1;b.Uy>=b.Jm&&(!__sf2Lan||__sf2Lan.canStep());)b.lyb++,__sf2Lan&&__sf2Lan.pre(),c(b.Jm),__sf2Lan&&__sf2Lan.post()'),
    # the VS screen ends only when both players have loaded the fight
    ("LAN wait at VS screen",
     r'case 3:if\(!this\.Im\)break;',
     'case 3:if(!this.Im||__sf2Lan&&!__sf2Lan.ready(this.Ng))break;'),
    ("LAN start at first fight update",
     r'case 5:if\(this\.Ng==null\)break;if\(!this\.Ng\.loaded\)\{this\.gd=6;this\.time=0;break\}\$a\.F\(\)\.update\(\);',
     'case 5:if(this.Ng==null)break;if(!this.Ng.loaded){this.gd=6;this.time=0;break}'
     '__sf2Lan&&__sf2Lan.start(this.Ng,J,Da);$a.F().update();'),
    # fighters are created in e2a: same seeds on both PCs from here on
    ("LAN seed at fight creation",
     r'e2a\(\)\{function a\(\)\{b\.bhb\(\);b\.loaded=!0\}',
     'e2a(){__sf2Lan&&__sf2Lan.reseed(J,Da,1);function a(){b.bhb();b.loaded=!0}'),
    # The fight's rules reseed the game's generator from the clock when a round stage changes (hu.kpb:
    # Da.AL). From round 2 on that happens inside a lockstep step, so each device seeded with its own time
    # and the next chance roll (v.Fdb: knock-down ...) differed. In a step the seed now comes from the
    # match's dice.
    ("LAN seed instead of the clock",
     r'static AL\(\)\{let a=fd\.getDate\(\)\.getTime\(\);',
     'static AL(){let a=__sf2Lan&&__sf2Lan.sw?__sf2Lan.rnd()*2147483647|0:fd.getDate().getTime();'),
    # the AI's random numbers (defense chances, move picks) come from Math.random -> seeded
    ("LAN seeded random",
     r'Imb\(\)\{return Math\.random\(\)\}',
     'Imb(){return __sf2Lan?__sf2Lan.rnd():Math.random()}'),
    # the "ROUND n" countdown starts after a 500 ms timer and the portal's ad-break hook (wall clock).
    # Over LAN it uses the frame clock instead (the same 0.5 s)
    ("LAN ends at the match result",
     r'wea\(a\)\{this\.rsb\(\);',
     'wea(a){__sf2Lan&&__sf2Lan.over(this);this.rsb();'),
    # the fight's result (1 = the left fighter won, 0 = lost) is the left side's view on both PCs;
    # the right player's PC shows it from the right side: "You win!" when the right fighter won
    # an item model hooks into a node (bone) of another model by name; the game threw only the bare
    # number 22 when that node is missing - now the error names it
    ("model node error",
     r'if\(g!=null\)d\.IXa\(g,f\.second\);else throw 22;',
     "if(g!=null)d.IXa(g,f.second);else throw new Error('model node not found: '+f.first);"),
    ("LAN result for the right player",
     r'static tD\(a,b,c,d,e,f,g,h\)\{h==null&&\(h=0\);',
     'static tD(a,b,c,d,e,f,g,h){h==null&&(h=0);__sf2Lan&&__sf2Lan.side==1&&(e===0||e===1)&&(e=1-e);'),
    ("LAN round banner",
     r'gg\.delay\(function\(\)\{J\.I\.zx\("break:round",function\(\)\{c\.PU=!0\}\)\},500\)',
     '__sf2Lan?(c.Nc+=.5,c.PU=!0):gg.delay(function(){J.I.zx("break:round",function(){c.PU=!0})},500)'),
]


MENU_JS = r"""// SF2 match menu. It shows before the game when no match is set, or with ?menu.
// The match it makes (match.json) stays in this tab (sessionStorage "sf2-config"), also over reloads.
// Two devices: sessionStorage "sf2-net" = {relay: the host's network server, role: host | join}.
// The host's menu opens the network server (server.py: POST /sf2-host; the APK opens its own).
(function(){
var D=window.__SF2MENU,R=window.__SF2DATA,touch=window.matchMedia&&matchMedia('(pointer: coarse)').matches;
var SLOTS=['Weapon','Ranged','Magic','Helm','Armor'];
var S={mode:null,type:null,fight:null,eclipse:false,difficulty:'',oppHuman:true,
  p:[{tpl:'Default',super:''},{tpl:'Man_Kungfu',super:'',tactic:'Standard'}],active:0,
  tab:'boss',weapon:'',q:'',location:'bamboo_grove',rounds:3,time:99,rules:[],zone:'',ftype:'',fq:''};
var MENU_KEEP=['mode','type','fight','eclipse','difficulty','oppHuman','p','active','tab','weapon','location','rounds','time','rules','zone','ftype'];
var css=`
#sf2m{position:fixed;inset:0;z-index:100000;background:radial-gradient(circle at 50% 0,#3a2a1a,#0b0806 70%);color:#eee;
 font:14px/1.35 system-ui,sans-serif;overflow:auto;-webkit-user-select:none;user-select:none}
#sf2m h1{margin:0;font-size:22px;letter-spacing:2px;color:#f3c66b;text-transform:uppercase}
#sf2m .bar{display:flex;gap:10px;align-items:center;padding:10px 14px;border-bottom:1px solid #4a3a26;position:sticky;top:0;background:#120d09ee;z-index:2}
#sf2m .sp{flex:1}
#sf2m button,#sf2m select,#sf2m input{font:inherit;color:#eee;background:#2a2017;border:1px solid #6b5333;border-radius:6px;padding:7px 11px}
#sf2m button{cursor:pointer}#sf2m button:hover{border-color:#f3c66b}
#sf2m button.go{background:#8a5a12;border-color:#f3c66b;color:#fff;font-weight:600}
#sf2m button.on{background:#f3c66b;color:#1a120a}
#sf2m .wrap{padding:14px;max-width:1200px;margin:0 auto}
#sf2m .cards{display:flex;gap:14px;flex-wrap:wrap;justify-content:center;margin-top:30px}
#sf2m .big{width:230px;padding:20px;border:1px solid #6b5333;border-radius:10px;background:#1e1610;cursor:pointer}
#sf2m .big:hover{border-color:#f3c66b}#sf2m .big b{display:block;font-size:17px;color:#f3c66b;margin-bottom:6px}
#sf2m .big.off{opacity:.4;pointer-events:none}
#sf2m .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(92px,1fr));gap:8px}
#sf2m .ch{border:2px solid #3c2e1e;border-radius:8px;background:#1b140e;cursor:pointer;text-align:center;padding:4px;font-size:11px;position:relative}
#sf2m .ch img,#sf2m .ch .ph{width:72px;height:72px;object-fit:cover;border-radius:50%;background:#000;display:block;margin:2px auto}
#sf2m .ph{display:flex!important;align-items:center;justify-content:center;font-size:28px;color:#6b5333}
#sf2m .ch.p0{border-color:#4aa3ff}#sf2m .ch.p1{border-color:#ff5a4a}
#sf2m .ch .tag{position:absolute;top:2px;left:4px;font-weight:700;font-size:11px}
#sf2m .ch .w{color:#a8916b;font-size:10px}
#sf2m .two{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
#sf2m .pl{border:1px solid #4a3a26;border-radius:10px;padding:10px;background:#16100b;cursor:pointer}
#sf2m .pl.act{border-color:#f3c66b;box-shadow:0 0 0 1px #f3c66b}
#sf2m .pl .hd{display:flex;gap:10px;align-items:center}
#sf2m .pl .hd img,#sf2m .pl .hd .ph{width:64px;height:64px;border-radius:50%;object-fit:cover}
#sf2m .pl h3{margin:0;color:#f3c66b;text-transform:capitalize}
#sf2m .its{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
#sf2m .it{width:86px;font-size:10px;text-align:center;color:#c9b48d}
#sf2m .it div{height:34px;background:#0c0906;border-radius:4px;display:flex;align-items:center;justify-content:center}
#sf2m .it img{max-width:82px;max-height:32px}
#sf2m .fx,#sf2m .it .fx{color:#7fd0ff;font-size:10px;margin-top:2px;height:auto;background:none;display:block}
#sf2back{position:fixed;top:12px;left:12px;z-index:100001;font:bold 20px sans-serif;color:#f3c66b;background:rgba(0,0,0,.65);
  border:1px solid #f3c66b;border-radius:14px;padding:7px 16px;cursor:pointer}
#sf2m .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:10px 0}
#sf2m .ench span{white-space:nowrap}
#sf2m .warn{color:#e8a33c}
#sf2m .list{max-height:62vh;overflow:auto;border:1px solid #3c2e1e;border-radius:8px}
#sf2m .bt{padding:6px 10px;background:#1e1610;color:#f3c66b;font-weight:600;position:sticky;top:0}
#sf2m .fi{padding:6px 10px;border-top:1px solid #2a2017;cursor:pointer;display:flex;gap:8px}
#sf2m .fi:hover{background:#2a2017}#sf2m .fi.sel{background:#5a3d10}
#sf2m .badge{font-size:10px;padding:1px 6px;border-radius:8px;background:#3a2b1a;color:#e0c48a}
#sf2m .err{color:#ff8a7a;white-space:pre-wrap;margin:8px 0}
#sf2m textarea{width:100%;height:50vh;font:12px monospace;background:#0c0906;color:#eee;border:1px solid #6b5333;border-radius:6px}
#sf2m .muted{color:#a8916b;font-size:12px}
#sf2m .by{position:fixed;right:10px;bottom:6px;font-size:11px;color:#a8916b;opacity:.45;pointer-events:none}
@media (max-width:700px){#sf2m .two{grid-template-columns:1fr}#sf2m .big{width:100%}}`;
function el(h){var d=document.createElement('div');d.innerHTML=h;return d.firstElementChild}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function cap(s){return String(s||'').toLowerCase().replace(/_/g,' ').replace(/(^|[\s(\-])\w/g,function(c){return c.toUpperCase()})}   // capital first letters (not after an apostrophe: Lynx's)
var CH={};D.characters.forEach(function(c){CH[c.name]=c});
function dname(c){return LANG=='tr'&&c.displayTr||c.display}   // a character's name in the menu's language
function avatar(c,cls){var u=c&&D.avatars[c.avatar];return u?'<img class="'+(cls||'')+'" src="'+u+'" loading="lazy">':'<div class="ph '+(cls||'')+'">'+esc((c&&c.display||'?')[0])+'</div>'}
var root,body,err='';
// Turkish on a Turkish device, English elsewhere (the game follows the device language too)
var TR={"Extra perk": "Ekstra perk", "Fighter perks": "Dövüşçü perkleri", "Elixirs": "İksirler", "Fight rules": "Dövüş kuralları", "area": "alan", "win": "kazanma", "Your name": "Adın", "Picture": "Resim", "the classic shadow": "klasik gölge", "Perk tree": "Yetenek ağacı", "Change gear and perk tree in the dojo first": "Önce dojoda eşyaları ve yetenek ağacını değiştir", "First fight: pick your gear and perk tree in the dojo, its fight button starts the fight. They are kept for the next fights.": "İlk dövüş: eşyalarını ve yetenek ağacını dojoda seç, dojonun dövüş butonu dövüşü başlatır. Sonraki dövüşler için saklanır.", "My own fighter": "Kendi dövüşçüm", "Try": "Dene", "Try it in the dojo": "Dojoda dene", "What they do": "Ne işe yarıyorlar", "no ranged": "fırlatma yok", "no magic": "büyü yok", "no helm": "kask yok", "no armor": "zırh yok", "no weapon": "silah yok", "Normal game": "Normal oyun", "The game as it is, with its own save on this device (export / import it with the \u2630 button). Unlimited energy.": "Oyunun kendisi, bu cihazda kendi kaydıyla (\u2630 butonuyla dışa / içe aktarılır). Enerji sınırsız.", "Play": "Oyna", "With cheats: money and gems": "Hileli: para ve elmas", "Back": "Geri", "Traits": "Özellikler", "Enchantments": "Tılsımlar", "Works only with this armor:": "Sadece bu zırhla çalışır:", "Weapon": "Silah", "Close": "Kapat","Form": "Form", "template": "şablon", "Bosses": "Bosslar", "Underworld": "Yeraltı", "Android extra": "Android ekleri", "phase": "aşama", "Zone": "Bölge","own": "kendi", "others": "diğerleri", "Ability": "Yetenek", "Up when charged": "dolunca Yukarı", "Ability key": "Yetenek tuşu", "Ranged": "Fırlatma", "Magic": "Büyü", "Helm": "Kask", "Armor": "Zırh", "none": "yok","Create match": "Maç kur", "You choose the match and play on the left. Opens this device to the other player.": "Maçı sen seçer, solda oynarsın. Bu cihazı diğer oyuncuya açar.", "You choose the match and play on the left, on the server this page came from:": "Maçı sen seçer, solda oynarsın; bu sayfanın geldiği sunucuda:", "Share the game": "Oyunu paylaş", "Other devices (iPhones too) open the game from this one in their browser, then play through it. This device does not have to play.": "Diğer cihazlar (iPhone da) oyunu bu cihazdan tarayıcılarında açar ve bunun üzerinden oynar. Bu cihazın oynaması gerekmez.", "Game port": "Oyun portu", "Network port": "Ağ portu", "Share": "Paylaş", "Other devices open": "Diğer cihazlar şunu açsın:", "Keep this app open while others play.": "Diğerleri oynarken bu uygulamayı açık tut.","Shadow Fight 2": "Shadow Fight 2", "Single player": "Tek oyuncu", "You against the AI.": "Sen, yapay zekâya karşı.", "Two players, this PC": "İki oyuncu, bu PC", "Both on one keyboard. Not on phones.": "İkiniz bir klavyede. Telefonda yok.", "Two players, network": "İki oyuncu, ağ", "Each on their own device, through a relay server.": "Herkes kendi cihazında, ağ sunucusu üzerinden.", "Host": "Host", "Opens this device to the other player. You choose the match and play on the left.": "Bu cihazı diğer oyuncuya açar. Maçı sen seçersin, solda oynarsın.", "Port": "Port", "Join": "Katıl", "The host chooses the match; you pick your fighter and play on the right.": "Maçı host seçer; sen dövüşçünü seçip sağda oynarsın.", "host address, e.g. 192.168.1.20:8081": "host adresi, ör. 192.168.1.20:8081", "Choose the match": "Maçı seç", "Story fight": "Hikâye dövüşü", "A fight of the game as it is: its opponents, rules and place.": "Oyundaki bir dövüş olduğu gibi: rakipleri, kuralları, mekânı.", "Free fight": "Serbest dövüş", "Pick the fighters, the place and the rules.": "Dövüşçüleri, mekânı ve kuralları seç.", "No balancing (level 52, every item)": "Dengeleme yok (seviye 52, her eşya)", "No balancing (AI as its template)": "Dengeleme yok (yapay zekâ şablonu gibi)", "Very Easy": "Çok kolay", "Easy": "Kolay", "Middle": "Orta", "Hard": "Zor", "Very Hard": "Çok zor", "All zones": "Tüm bölgeler", "All types": "Tüm türler", "search": "ara", "Difficulty": "Zorluk", "The other player plays the opponents": "Rakipleri diğer oyuncu oynar", "You fight as yourself: pick your gear in the dojo, then its fight button starts the fight. Difficulty sets your level so that the game rates the fight that way.": "Kendin olarak dövüşürsün: eşyalarını dojoda seç, dojonun dövüş butonu dövüşü başlatır. Zorluk, oyunun dövüşü öyle değerlendireceği seviyeye seni ayarlar.", "Player 1": "Oyuncu 1", "Player 2": "Oyuncu 2", "Opponent (AI)": "Rakip (yapay zekâ)", "picks on their own device": "kendi cihazında seçiyor", "My own fighter: I pick the gear in the dojo": "Kendi dövüşçüm: eşyaları dojoda seçerim", "Own fighter": "Kendi dövüşçün", "gear from the dojo; the match starts from its fight button": "eşyalar dojodan; maç dojonun dövüş butonuyla başlar", "none": "yok", "Tactic": "Taktik", "Your fighter": "Dövüşçün", "The host chose:": "Host seçti:", "rounds": "raund", "You play on the right.": "Sen sağda oynarsın.", "Place": "Mekân", "Rounds": "Raund", "Seconds": "Saniye", "Bosses": "Bosslar", "Characters": "Karakterler", "Fighters": "Dövüşçüler", "All weapons": "Tüm silahlar", "Random": "Rastgele", "Tap a card for the highlighted player.": "Seçili oyuncu için bir karta dokun.", "What the menu made. Change anything (see the config guide) and start.": "Menünün oluşturduğu maç. İstediğini değiştir ve başlat.", "Fight with this JSON": "Bu JSON ile dövüş", "Fight!": "Dövüş!", "Others join with": "Diğerleri şununla katılır:", "copied": "kopyalandı", "Pick a fight from the list.": "Listeden bir dövüş seç.", "waiting for the other player to pick a fighter ...": "diğer oyuncunun dövüşçü seçmesi bekleniyor ...", "waiting for the host ...": "host bekleniyor ...", "connected - waiting for the host to choose a match ...": "bağlandı - host'un maç seçmesi bekleniyor ...", "connecting to": "bağlanılıyor:", "the network server does not answer": "ağ sunucusu cevap vermiyor", "no host at": "bu adreste host yok:", "(has the host chosen Host yet?)": "(host, Host'a bastı mı?)", "could not open the network server:": "ağ sunucusu açılamadı:", "random": "rastgele"};
// the page came from another device (server.py --share, or the APK's "Share the game")
var REMOTE=!window.SF2Native&&!/^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname);
var LANG=(navigator.language||'').toLowerCase().indexOf('tr')==0?'tr':'en';
function T(s){return LANG=='tr'&&TR[s]||s}
function copy(text){                              // copies the host address (http pages need the old way)
  var ok=function(){S.copied=true;draw();setTimeout(function(){S.copied=false;draw()},1500)};
  if(navigator.clipboard&&window.isSecureContext)return navigator.clipboard.writeText(text).then(ok,function(){});
  var t=document.createElement('textarea');t.value=text;t.style.cssText='position:fixed;opacity:0';document.body.appendChild(t);
  t.select();try{document.execCommand('copy');ok()}catch(e){}document.body.removeChild(t)}
// ---------------------------------------------------------------- match.json: merge and check
// One check for every source: the menu, the JSON view, server.py and the APK. Errors show in the menu
// and go to server.py's window (or Android's log). The game merges with __sf2Merge too.
window.__sf2Merge=function(base,over){            // match.json over the defaults: objects are merged, other values replaced
  var out=JSON.parse(JSON.stringify(base));
  for(var k in over||{}){var v=over[k];
    out[k]=v&&typeof v=='object'&&!Array.isArray(v)&&out[k]&&typeof out[k]=='object'&&!Array.isArray(out[k])?__sf2Merge(out[k],v):v}
  return out};
window.__sf2Check=function(user,net){             // the errors in a match.json ([] = none);
                                                  // net: a two-device match
  var K=R.check,errs=[],same=function(a,b){return JSON.stringify(a)===JSON.stringify(b)};
  var has=function(o,v){return Array.isArray(o)?o.indexOf(v)>=0:Object.prototype.hasOwnProperty.call(o,v)};
  var obj=function(v){return v&&typeof v=='object'&&!Array.isArray(v)};
  if(!obj(user))return['match.json: an object { ... }'];
  (function walk(base,over,path){for(var k in over){
    if(!has(base,k)){errs.push('unknown setting "'+path+k+'" (valid: '+Object.keys(base).join(', ')+')');continue}
    if(obj(base[k])&&Object.keys(base[k]).length)obj(over[k])?walk(base[k],over[k],path+k+'.'):errs.push(path+k+': an object { ... }')}})(R.defaults,user,'');
  if(errs.length)return errs;
  var c=__sf2Merge(R.defaults,user),game=c.fight!=null,sides=['left','right'];
  if(game&&!(user.right&&'human' in user.right))c.right.human=false;
  sides.forEach(function(sn){['items','perks','enchantments'].forEach(function(k){Array.isArray(c[sn][k])||errs.push(sn+'.'+k+': a list [ ... ]')})});
  if(errs.length)return errs;
  if(game){
    has(K.fights,c.fight)||errs.push('fight: unknown "'+c.fight+'" (server.py --list battles)');
    for(var k in c)!has(K.gameFight,k)&&!same(c[k],R.defaults[k])&&errs.push(k+': a custom-match setting; a game fight ("fight") has the game\'s own');
    sides.forEach(function(sn){for(var k in c[sn])!has(K.gameSide[sn],k)&&!same(c[sn][k],R.defaults[sn][k])&&
      errs.push(sn+'.'+k+': a custom-match setting; in a game fight only '+K.gameSide[sn].join(', ')+' apply to '+sn)});
    var sl=c.left.items.map(function(n){return K.items[n]});
    sl.some(function(t,i){return !has(K.slots,t)||sl.indexOf(t)!=i})&&errs.push('left.items: in a game fight at most one item per slot ('+K.slots.join(', ')+')')}
  var known={template:[R.templates,'templates'],items:[K.items,'items'],perks:[K.perks,'perks'],enchantments:[K.enchantments,'perks'],tactic:[K.tactics,'tactics']};
  sides.forEach(function(sn){var s=c[sn];
    for(var key in known){if(key=='tactic'&&s.human||game&&key=='template')continue;
      (Array.isArray(s[key])?s[key]:[s[key]]).forEach(function(v){v!=null&&!has(known[key][0],v)&&
        errs.push(sn+'.'+key+': unknown "'+v+'" (server.py --list '+known[key][1]+')')})}
    s.align!=null&&!(Array.isArray(s.align)&&s.align.length&&s.align.every(function(d){return Array.isArray(d)&&d.length==2}))&&
      errs.push(sn+'.align: a list of [factor, shift] pairs, e.g. [[0, 0]]');
    s.items.forEach(function(v){K.why[v]&&errs.push(sn+'.items: "'+v+'" '+K.why[v])});
    var o=s.own;o&&((o.items||[]).forEach(function(v){v&&!K.items[String(v).split('@')[0]]&&errs.push(sn+'.own.items: no item "'+v+'"')}),
      (o.perks||[]).forEach(function(v){K.perks.indexOf(String(v).split('@')[0])<0&&errs.push(sn+'.own.perks: no perk "'+v+'"')}))});
  sides.forEach(function(sn){c[sn].template&&has(K.broken,c[sn].template)&&
    errs.push(sn+'.template: "'+c[sn].template+'" is left out: an item\'s 3D model is missing in this game version, or it is only a renamed copy of another template')});
  sides.forEach(function(sn){var s=c[sn];if(!s.dojo)return;
    game?errs.push(sn+'.dojo: a game fight always starts the player in the dojo'):
    !s.human?errs.push(sn+'.dojo: only a human picks gear in the dojo'):
    !net&&c.left.human&&c.right.human&&errs.push(sn+'.dojo: two players on one screen share no dojo (champions only)')});
  if(c.difficulty!=null)!has(R.difficulties,c.difficulty)?errs.push('difficulty: one of '+R.difficulties.join(', ')):
    !game&&c.left.human&&c.right.human&&errs.push('difficulty: a custom match sets the AI side\'s strength; both sides are human');
  c.language!=null&&!has(K.languages,c.language)&&errs.push('language: "'+c.language+'" - the game has '+K.languages.join(', '));
  has(K.locations,c.location)||errs.push('location: unknown "'+c.location+'" (server.py --list locations)');
  return errs};
function report(msg){                             // -> server.py's window / Android's log
  if(window.SF2Native)return SF2Native.log('match',msg);
  try{fetch('/sf2-log',{method:'POST',body:JSON.stringify({kind:'match',msg:msg}),keepalive:true})}catch(x){}}
function refuse(c,errs){                          // a match with errors: back to the JSON view
  errs.forEach(report);S.json=c;S.view='json';err=errs.join('\n')}

function config(){                               // the menu's choices as match.json
  var c={},net=S.mode=='lan';
  if(S.type=='story'){c.fight=S.fight;S.eclipse&&(c.eclipse=true);S.difficulty&&(c.difficulty=S.difficulty);
    c.right={human:net&&S.oppHuman};return c}
  c.left=side(0,true);c.right=net?{human:true}:side(1,S.mode!='single');   // network: the other player picks it
  c.location=S.location;D.music[S.location]&&(c.music=D.music[S.location]);c.rounds=+S.rounds;c.round_time=+S.time;
  S.rules=S.rules.filter(function(k){return D.rules[k]});S.rules.length&&(c.rules=S.rules.map(ruleXml));
  S.mode=='single'&&S.difficulty&&(c.difficulty=S.difficulty);return c}

// the own fighter, kept on this device: name and picture (menu), gear and perk-screen perks (saved by the
// game after the dojo: __sf2SaveOwn)
function ownGet(){try{return JSON.parse(localStorage.getItem('sf2-own')||'{}')||{}}catch(e){return{}}}
function ownSet(k,v){var o=ownGet();o[k]=v;try{localStorage.setItem('sf2-own',JSON.stringify(o))}catch(e){}}
function side(i,human){                          // a player card as a match.json side
  var p=S.p[i];if(human&&p.dojo){var en=SLOTS.map(function(t){return(p.ench||{})[t]}).filter(Boolean);   // own fighter: saved, or gear from the dojo; any Super,
    var sk=p.super&&D.abilitySkeleton&&D.abilitySkeleton[p.super];   // a Super with a skeleton of its own wears it
    var pk=[p.super,p.extra].filter(Boolean),xs=extraSets(p),o=ownGet();
    return Object.assign({human:true,template:'Default'},o.name?{name:o.name}:{},o.avatar?{avatar:o.avatar}:{},
      p.edit||!o.items?{dojo:true}:{own:{items:o.items,perks:o.perks||[]}},   // the first time and "edit": the dojo
      pk.length?{perks:pk}:{},xs?{perk_sets:xs}:{},sk?{items:[sk]}:{},en.length?{enchantments:en}:{})}   // an enchantment per slot
  var c=CH[p.tpl]||{},s={human:human,template:c.template||p.tpl},L=load(p),items=c.weaponItem?[c.weaponItem]:[];   // a story character: its template, name, picture, weapon
  var sk=p.super&&D.abilitySkeleton&&D.abilitySkeleton[p.super];   // a Super with a skeleton of its own: worn
  c.fname&&(s.name=c.fname,s.avatar=c.avatar);
  SLOTS.forEach(function(t,k){if(!k)return;var own=c.items&&c.items[k],v=L[t];   // only what differs from the template
    (own=='NoRanged'||own=='NoMagic')&&(own=null);
    (v||null)!==(own||null)&&items.push(v||{Ranged:'NoRanged',Magic:'NoMagic'}[t])});
  sk&&items.push(sk);  items=items.filter(Boolean);items.length&&(s.items=items);
  var F=form(p),perks=(p.super?[p.super]:[]).concat(F.traits||[]);perks.length&&(s.perks=perks);
  F.sets&&Object.keys(F.sets).length&&(s.perk_sets=F.sets);   // the traits' values in the form's fight
  F.ench&&F.ench.length&&(s.enchantments=F.ench);   // the form's enchantments (the game's fight gives them)
  !human&&(s.tactic=p.tactic||'Standard');return s}
function extraSets(p){                          // the extra perk's Set values (the game's own use of it), or null
  var v=p.extra&&D.extraPerks.fighter[p.extra],o={};return v&&Object.keys(v).length?(o[p.extra]=v,o):null}
function xmlAttrs(o){var a='';for(var k in o)a+=' '+k+'="'+esc(o[k])+'"';return a}
function ruleXml(k){                             // a fight rule (D.rules: the game's rule, for both fighters)
  return'<ComplexRule><Description Alias="'+esc(D.rules[k].alias)+'"/>'+D.rules[k].xml+'</ComplexRule>'}
function ruleName(k){var r=D.rules[k];return r?LANG=='tr'?r.nameTr:r.name:k}
function ruleText(k){var r=D.rules[k];return r?LANG=='tr'?r.textTr:r.text:''}
function perkName(k){return PK(k)[0]}
function ownCard(i,p,who,dojo){                 // the own fighter: name, picture, saved gear and perks, Super ...
  var o=ownGet(),ed=p.edit||!o.items,av=o.avatar?{avatar:o.avatar}:CH.Default;
  var pics=D.characters.filter(function(c){return c.avatar&&D.avatars[c.avatar]}),seen={};
  var its=(o.items||[]).map(function(n,k){var it=n&&D.items[n.split('@')[0]];it=it&&R.start.indexOf(n.split('@')[0])<0?it:null;
    return'<div class="it"><div>'+(it&&it.img?'<img src="'+it.img+'">':'&ndash;')+'</div>'+esc(it?cap(it.display):T('no '+SLOTS[k].toLowerCase()))+'</div>'}).join('');
  var lv={};(o.perks||[]).forEach(function(x){var a=x.split('@');lv[a[0]]=Math.max(lv[a[0]]||0,+a[1]||1)});   // Uc: the level, from 1
  var tree=Object.keys(lv).map(function(k){return esc(perkName(k))+(lv[k]>1?' '+lv[k]:'')});
  return'<div class="pl act" data-player="'+i+'"><div class="hd">'+avatar(av)+'<div><div class="muted">'+who+'</div>'+
    '<input data-ownname="1" maxlength="20" placeholder="'+T('Your name')+'" value="'+esc(o.name||'')+'">'+
    '<div class="muted">'+T('Picture')+' <select data-ownav="1"><option value="">'+T('the classic shadow')+'</option>'+
      pics.filter(function(c){return!seen[c.avatar]&&(seen[c.avatar]=1)}).map(function(c){return'<option value="'+esc(c.avatar)+'"'+(o.avatar==c.avatar?' selected':'')+'>'+esc(cap(dname(c)))+'</option>'}).join('')+'</select></div></div></div>'+
    (o.items?'<div class="its">'+its+'</div>'+(tree.length?'<div class="row fx">'+T('Perk tree')+': '+tree.join(', ')+'</div>':''):'')+
    '<div class="row">'+abilitySelect(i,{forms:Object.keys(D.abilities).map(function(k){return{ability:k}})},p)+armorNote(p)+'</div>'+   // any boss ability
    extraSelect(i,p)+enchSelect(i,p)+describe([p.super].concat(SLOTS.map(function(t){return(p.ench||{})[t]}),[p.extra]),'',extraSets(p))+
    (o.items?'<label class="row"><input type="checkbox" data-edit="'+i+'"'+(p.edit?' checked':'')+'> '+T('Change gear and perk tree in the dojo first')+'</label>':
      '<div class="row muted">'+T('First fight: pick your gear and perk tree in the dojo, its fight button starts the fight. They are kept for the next fights.')+'</div>')+
    (ed&&o.items?'<div class="row muted">'+T('gear from the dojo; the match starts from its fight button')+'</div>':'')+dojo+'</div>'}
function extraSelect(i,p){                       // the own fighter's one extra perk (D.extraPerks.fighter)
  var F=Object.keys(D.extraPerks.fighter),el=F.filter(function(k){return/^PERK_ELIXIR/.test(k)}),
    opt=function(k){return'<option value="'+k+'"'+(p.extra==k?' selected':'')+'>'+esc(perkName(k))+'</option>'};
  return'<div class="row">'+T('Extra perk')+' <select data-extra="'+i+'"><option value="">'+T('none')+'</option><optgroup label="'+
    T('Fighter perks')+'">'+F.filter(function(k){return el.indexOf(k)<0}).sort(function(a,b){return perkName(a)<perkName(b)?-1:1}).map(opt).join('')+
    '</optgroup><optgroup label="'+T('Elixirs')+'">'+el.map(opt).join('')+'</optgroup></select></div>'}
function rulesBox(){                             // fight rules for both fighters (D.rules); one area rule, one way to win
  var R=D.rules;S.rules=S.rules.filter(function(k){return R[k]});
  return'<details class="row"'+(S.rules.length?' open':'')+'><summary>'+T('Fight rules')+(S.rules.length?' ('+S.rules.length+')':'')+'</summary>'+
    Object.keys(R).sort(function(a,b){return ruleName(a)<ruleName(b)?-1:1}).map(function(k){
    return'<label style="display:inline-block;margin:4px 14px 4px 0" title="'+esc(ruleText(k))+'"><input type="checkbox" data-rule="'+k+'"'+
      (S.rules.indexOf(k)>=0?' checked':'')+'> '+esc(ruleName(k))+(R[k].area?' <span class="muted">('+T('area')+')</span>':R[k].win?' <span class="muted">('+T('win')+')</span>':'')+'</label>'}).join('')+
    S.rules.map(function(k){return'<div class="muted"><b>'+esc(ruleName(k))+'</b>: '+esc(ruleText(k))+'</div>'}).join('')+'</details>'}
function pickChar(p,name){var c=CH[name]||{};p.tpl=name;p.form=c.form|0;p.super=(form(p).ability)||''}
function form(p){var c=CH[p.tpl]||{},f=c.forms||[];if(p.form==null||!f[p.form])p.form=c.form|0;return f[p.form]||{load:{}}}   // its chosen form
function load(p){return form(p).load}
function go(c,net){
  window.__sf2TryUndo&&__sf2TryUndo();          // a tried form was only for looking; keep the match and start the game
  sessionStorage.setItem('sf2-config',JSON.stringify(c));
  var keep={};MENU_KEEP.forEach(function(k){keep[k]=S[k]});   // restore() brings this back after the match
  sessionStorage.setItem('sf2-menu',JSON.stringify(keep));
  net?sessionStorage.setItem('sf2-lobby',JSON.stringify({role:net.role,server:S.server,hostAddr:S.hostAddr,relay:S.relay})):
    sessionStorage.removeItem('sf2-lobby');
  net?sessionStorage.setItem('sf2-net',JSON.stringify(net)):sessionStorage.removeItem('sf2-net');
  location.href=location.pathname}
function start(json){
  err='';var c;try{c=json?JSON.parse(json):config()}catch(e){err='JSON: '+e.message;return draw()}
  var errs=__sf2Check(c,S.mode=='lan');if(errs.length){refuse(c,errs);return draw()}
  if(S.mode!='lan')return go(c,null);
  // host, free fight: wait for the other player's side, then both games start the same match
  var hostNet={relay:S.server,role:'host'};
  lobby('lobby-host',function(ws){S.hws=ws;
    if(c.fight||json)return ws.send(JSON.stringify({t:'cfg',config:c})),go(c,hostNet);
    S.wait=T('waiting for the other player to pick a fighter ...');draw();ws.send(JSON.stringify({t:'setup',config:c}))},
    function(m,ws){if(m.t!='pick')return;var full=JSON.parse(JSON.stringify(c));full.right=m.right;
      var e=__sf2Check(full,true);if(e.length){S.wait=null;refuse(full,e);return draw()}
      ws.send(JSON.stringify({t:'cfg',config:full}));go(full,hostNet)})}
function restore(){                              // after a match: the menu as it was; on the network the lobby opens again
  var k=sessionStorage.getItem('sf2-menu'),l=sessionStorage.getItem('sf2-lobby');   // (the host's screen, the other player joins)
  sessionStorage.removeItem('sf2-menu');sessionStorage.removeItem('sf2-lobby');
  try{k&&Object.assign(S,JSON.parse(k))}catch(e){}
  try{l=l&&JSON.parse(l)}catch(e){l=null}
  if(!l)return;
  S.mode='lan';
  if(l.role=='host'){S.host=true;S.server=l.server;S.hostAddr=l.hostAddr}
  else{S.type=null;S.relay=l.relay;setTimeout(join)}}
function unhost(){                               // the host left: close its network server (not a shared one)
  if(REMOTE||S.shareUrl)return;
  window.SF2Native?SF2Native.unhost&&SF2Native.unhost():fetch('sf2-unhost',{method:'POST'}).catch(function(){})}
function lobby(role,open,msg){                   // the two menus talk through the network server before the match
  var addr=role=='lobby-host'?S.server:S.relay,ws;
  try{ws=new WebSocket('ws://'+addr+'/sf2-lan?role='+role)}catch(e){err=String(e);S.wait=S.joining=null;return draw()}
  ws.onopen=function(){open(ws)};
  ws.onerror=function(){S.wait=S.joining=null;err=role=='lobby-host'?T('the network server does not answer'):
    T('no host at')+' '+S.relay+' '+T('(has the host chosen Host yet?)');draw()};
  ws.onmessage=function(e){var m=JSON.parse(e.data);msg(m,ws)}}
function host(){                                 // host a match: on this device's network server, or on the page's server
  err='';var done=function(r){if(r.error){err=r.error;return draw()}S.server='127.0.0.1:'+r.port;S.hostAddr=r.lan;S.host=true;draw()};
  if(REMOTE){S.server=S.hostAddr=location.hostname+':'+(S.netPort||8081);S.host=true;return draw()}
  if(window.SF2Native)return done(JSON.parse(SF2Native.host(+S.netPort||0)));   // the APK opens it itself
  fetch('sf2-host',{method:'POST',body:JSON.stringify({port:+S.netPort||0})}).then(function(r){return r.json()}).then(done,
    function(e){err=T('could not open the network server:')+' '+e;draw()})}
function share(){                                // the APK serves the game to other devices' browsers (and opens the network server)
  err='';var r=JSON.parse(SF2Native.share(+S.sharePort||8080,+S.netPort||0));if(r.error){err=r.error;return draw()}
  S.shareUrl=r.url;S.server='127.0.0.1:'+r.port;S.hostAddr=r.lan;draw()}
function join(){                                 // the other player picks its side after the host chose the match
  err='';S.joining=T('connecting to')+' '+S.relay+' ...';draw();
  lobby('lobby-join',function(ws){S.ws=ws;S.joining=T('connected - waiting for the host to choose a match ...');draw()},
    function(m,ws){
      if(m.t=='setup'){S.setup=m.config;S.type='free';S.active=1;S.joining=S.wait=null;draw()}
      if(m.t=='cfg'){ws.close();go(m.config,{relay:S.relay,role:'join'})}})}
function pick(){                                 // sends this side to the host
  S.ws.send(JSON.stringify({t:'pick',right:side(1,true)}));S.wait=T('waiting for the host ...');draw()}

function bar(title,back){
  return '<div class="bar">'+(back?'<button data-a="back">&#8592;</button>':'')+'<h1>'+title+'</h1><div class="sp"></div>'+
    (S.host?'<span class="muted">'+T('Others join with')+' <button data-a="copy" title="copy">'+
      '<b style="color:#f3c66b">'+esc(S.hostAddr)+'</b> '+(S.copied?'&#10003; '+T('copied'):'&#10697;')+'</button></span>':'')+
    (S.mode&&!S.setup?'<button data-a="json">JSON</button>':'')+
    (S.wait?'':S.setup?'<button class="go" data-a="pick">'+T('Fight!')+'</button>':S.type?'<button class="go" data-a="start">'+T('Fight!')+'</button>':'')+'</div>'}

function screenMode(){
  var card=function(a,t,d,x){return'<div class="big'+(x||'')+'" '+a+'><b>'+T(t)+'</b>'+T(d)+'</div>'};
  return bar('Shadow Fight 2',S.inGame)+'<div class="wrap"><div class="cards">'+
    card('data-mode="single"','Single player','You against the AI.')+
    card('data-mode="same"','Two players, this PC','Both on one keyboard. Not on phones.',touch?' off':'')+
    card('data-mode="lan"','Two players, network','Each on their own device, through a relay server.')+
    '</div><div class="cards"><div class="big"><b>'+T('Normal game')+'</b>'+T('The game as it is, with its own save on this device (export / import it with the \u2630 button). Unlimited energy.')+
    '<div class="row"><button class="go" data-a="normal">'+T('Play')+'</button> <button data-a="cheat">'+T('With cheats: money and gems')+'</button></div></div></div></div>'}

function screenNet(){
  return bar(T('Two players, network'),1)+'<div class="wrap">'+
    '<div class="err">'+esc(err)+'</div>'+(S.joining?'<div class="row">'+esc(S.joining)+'</div>':'')+
    '<div class="cards"><div class="big"><b>'+T('Create match')+'</b>'+(REMOTE?T('You choose the match and play on the left, on the server this page came from:')+
      ' <b>'+esc(location.hostname+':'+(S.netPort||8081))+'</b>':T('You choose the match and play on the left. Opens this device to the other player.')+
      '<div class="row">'+T('Port')+' <input data-k="netPort" type="number" min="1024" max="65535" style="width:90px" value="'+esc(S.netPort||'')+'"></div>')+
    '<button class="go" data-net="host">'+T('Create match')+'</button></div>'+
    '<div class="big"><b>'+T('Join')+'</b>'+T('The host chooses the match; you pick your fighter and play on the right.')+
    '<div class="row"><input data-k="relay" value="'+esc(S.relay)+
    '" placeholder="'+T('host address, e.g. 192.168.1.20:8081')+'" style="width:100%"></div><button class="go" data-net="join">'+T('Join')+'</button></div>'+
    (window.SF2Native?'<div class="big"><b>'+T('Share the game')+'</b>'+T('Other devices (iPhones too) open the game from this one in their browser, then play through it. This device does not have to play.')+
      '<div class="row">'+T('Game port')+' <input data-k="sharePort" type="number" min="1024" max="65535" style="width:90px" value="'+esc(S.sharePort||8080)+'"> '+
      T('Network port')+' <input data-k="netPort" type="number" min="1024" max="65535" style="width:90px" value="'+esc(S.netPort||'')+'"></div>'+
      (S.shareUrl?'<div class="row">'+T('Other devices open')+' <button data-a="copyurl"><b style="color:#f3c66b">'+esc(S.shareUrl)+'</b> '+
        (S.copied?'&#10003; '+T('copied'):'&#10697;')+'</button></div><div class="muted">'+T('Keep this app open while others play.')+'</div>':
        '<button class="go" data-net="share">'+T('Share')+'</button>')+'</div>':'')+'</div></div>'}

function screenType(){
  var card=function(a,t,d){return'<div class="big" '+a+'><b>'+T(t)+'</b>'+T(d)+'</div>'};
  return bar(T('Choose the match'),1)+'<div class="wrap"><div class="cards">'+
    card('data-type="story"','Story fight','A fight of the game as it is: its opponents, rules and place.')+
    card('data-type="free"','Free fight','Pick the fighters, the place and the rules.')+'</div></div>'}

var DIFF=['','very_easy','easy','middle','hard','very_hard'];
function diffSelect(){return'<select data-k="difficulty">'+DIFF.map(function(d){return'<option value="'+d+'"'+(S.difficulty==d?' selected':'')+'>'+T(d?cap(d):S.type=='story'?'No balancing (level 52, every item)':'No balancing (AI as its template)')+'</option>'}).join('')+'</select>'}

function screenStory(){
  var zones=[],types=[];D.fights.forEach(function(f){var z=f.id.split('/')[0];zones.indexOf(z)<0&&zones.push(z);types.indexOf(f.type)<0&&types.push(f.type)});
  var q=S.fq.toLowerCase(),rows='',last='';
  D.fights.forEach(function(f){
    if(S.zone&&f.id.indexOf(S.zone+'/')!=0||S.ftype&&f.type!=S.ftype)return;
    var txt=(f.id+' '+f.title+' '+f.opponents.join(' ')+' '+f.location).toLowerCase();if(q&&txt.indexOf(q)<0)return;
    var b=f.id.split('/').slice(0,2).join(' / ');
    b!=last&&(rows+='<div class="bt">'+esc(b)+(f.title?' &middot; '+esc(f.title):'')+'</div>',last=b);
    rows+='<div class="fi'+(S.fight==f.id?' sel':'')+'" data-fight="'+esc(f.id)+'"><span>'+esc(f.id.split('/')[2])+'</span><span class="badge">'+esc(f.type)+'</span>'+
      '<span class="sp">'+esc(f.opponents.map(function(o){return o=='?'?T('random'):cap(o)}).join(', '))+'</span><span class="muted">'+esc(cap(f.location))+'</span></div>'});
  return bar(T('Story fight'),1)+'<div class="wrap"><div class="row">'+
    '<select data-k="zone"><option value="">'+T('All zones')+'</option>'+zones.map(function(z){return'<option value="'+z+'"'+(S.zone==z?' selected':'')+'>'+(z=='ZONE_RAID'?T('Underworld'):z=='ZONE_ANDROID'?T('Android extra'):z)+'</option>'}).join('')+'</select>'+
    '<select data-k="ftype"><option value="">'+T('All types')+'</option>'+types.map(function(z){return'<option'+(S.ftype==z?' selected':'')+'>'+z+'</option>'}).join('')+'</select>'+
    '<input data-k="fq" placeholder="'+T('search')+'" value="'+esc(S.fq)+'">'+
    '<label><input type="checkbox" data-k="eclipse"'+(S.eclipse?' checked':'')+'> Eclipse</label> '+T('Difficulty')+' '+diffSelect()+
    (S.mode=='lan'?'<label><input type="checkbox" data-k="oppHuman"'+(S.oppHuman?' checked':'')+'> '+T('The other player plays the opponents')+'</label>':'')+
    '</div><div class="muted">'+T('You fight as yourself: pick your gear in the dojo, then its fight button starts the fight. Difficulty sets your level so that the game rates the fight that way.')+'</div>'+
    (S.wait?'<div class="row"><b>'+esc(S.wait)+'</b></div>':'')+
    '<div class="err">'+esc(err)+'</div><div class="list">'+rows+'</div></div>'}

function mine(i){return S.setup?i==1:S.mode=='lan'?i==0:true}   // the cards this screen picks
function canDojo(i){return S.setup?i==1:S.mode=='lan'||S.mode=='single'?i==0:false}   // two players on one screen: no dojo
function player(i){
  var p=S.p[i],c=CH[p.tpl]||{},ai=i==1&&S.mode=='single';
  var dojo=canDojo(i)?'<label class="row"><input type="checkbox" data-dojo="'+i+'"'+(p.dojo?' checked':'')+
    '> '+T('My own fighter')+'</label>':'';
  var who=T(i?'Player 2':'Player 1');
  if(!mine(i))return'<div class="pl"><div class="hd"><div class="ph">?</div><div><div class="muted">'+who+
    '</div><h3>'+T('picks on their own device')+'</h3></div></div></div>';
  if(p.dojo)return ownCard(i,p,who,dojo);
  var L=load(p),tr=form(p).traits||[],en=form(p).ench||[],pk=PK;   // a trait or enchantment: [name, item slot or ""]
  var fx=function(slot){return(slot?en:tr).filter(function(x){return pk(x)[1]==slot}).map(function(x){return esc(pk(x)[0])})};
  var its=SLOTS.map(function(t,k){var n=k?L[t]:c.items&&c.items[k],it=n&&R.start.indexOf(n)<0&&D.items[n],e=fx(t);   // start items: nothing worn
    return'<div class="it"><div>'+(it&&it.img?'<img src="'+it.img+'">':'&ndash;')+'</div>'+esc(it?cap(it.display):T('no '+t.toLowerCase()))+
      (e.length?'<div class="fx">'+e.join('<br>')+'</div>':'')+'</div>'}).join('');
  var own=fx('');
  return'<div class="pl'+(S.active==i?' act':'')+'" data-player="'+i+'"><div class="hd">'+avatar(c)+'<div><div class="muted">'+
    (ai?T('Opponent (AI)'):who)+'</div><h3>'+esc(cap(dname(c)))+'</h3><div class="muted">'+esc(c.weapon)+'</div></div></div>'+
    '<div class="its">'+its+'</div>'+(own.length?'<div class="row fx">'+T('Traits')+': '+own.join(', ')+'</div>':'')+
    describe([p.super].concat(tr,en),'',form(p).sets)+
    formSelect(i,c,p)+'<div class="row">'+abilitySelect(i,c,p)+
    (!ai&&mine(i)?' <button data-try="'+i+'">'+T('Try it in the dojo')+'</button>':'')+
    (ai?' '+T('Tactic')+' <select data-tactic="1">'+D.tactics.map(function(t){return'<option'+(p.tactic==t?' selected':'')+'>'+t+'</option>'}).join('')+'</select>':'')+'</div>'+dojo+'</div>'}

function formSelect(i,c,p){                     // a character as in the game: one of its forms, never mixed
  var f=c.forms||[];if(f.length<2)return'';
  var label=function(x){if(x.label=='template')return T('template');var a=x.label.split('/');   // ZONE_2/BOSS_HERMIT/6 -> Zone 2 · Boss Hermit · 6
    var f=a[2].split('#');
    return(a[0]=='ZONE_RAID'?T('Underworld'):a[0]=='ZONE_ANDROID'?T('Android extra'):a[0].replace('ZONE_',T('Zone')+' '))+' · '+cap(a[1].replace(/_/g,' ').toLowerCase())+' · '+f[0]+(f[1]?' · '+T('phase')+' '+f[1]:'')};
  var what=function(x){var L=x.load,o=['Ranged','Magic'].map(function(t){var it=L[t]&&D.items[L[t]];return it?cap(it.display):''}).filter(Boolean);
    x.ability&&o.push(cap(((D.abilities[x.ability]||[x.ability])[0]).replace(/^PERK_/,'').replace(/_/g,' ')));return o.length?' - '+o.join(', '):''};
  return'<div class="row">'+T('Form')+' <select data-form="'+i+'">'+f.map(function(x,k){
    return'<option value="'+k+'"'+(p.form==k?' selected':'')+'>'+esc(label(x)+what(x))+'</option>'}).join('')+'</select></div>'}
function describe(list,extra,sets){              // "What they do": the Super and the traits, each game text once;
  var pk=function(x){return PK(x,sets)},seen={},d=[];                         // an enchantment's strength depends on the item slot, so the slot is named
  list.forEach(function(x){if(!x)return;var k=pk(x)[0]+'\n'+pk(x)[2],sl=pk(x)[1];   // no game text: its name alone
    seen[k]?sl&&seen[k].slots.indexOf(sl)<0&&seen[k].slots.push(sl):d.push(seen[k]={x:x,slots:sl?[sl]:[]})});
  return d.length||extra?'<details class="row fx"><summary>'+T('What they do')+'</summary>'+(extra||'')+
    d.map(function(e){var x=e.x;return'<div><b>'+esc(x==list[0]&&D.abilities[x]?'Super - '+cap(abilityName(x)):pk(x)[0]+
      (e.slots.length?' ('+e.slots.map(T).join(', ')+')':''))+'</b>'+(pk(x)[2]?': '+esc(pk(x)[2]):'')+'</div>'}).join('')+'</details>':''}
function armorNote(p){                         // the armor a Super needs, if any (D.abilityArmor)
  var a=p.super&&D.abilityArmor&&D.abilityArmor[p.super],it=a&&D.items[a];
  return a?' <span class="warn">'+T('Works only with this armor:')+' '+esc(it?cap(it.display):a)+'</span>':''}
function PK(x,sets){                            // a perk's [name, item slot, description] in the menu's language where the game has it;
  var e=D.perks[x]||[cap(x.replace(/^(BOSS_)?PERK_/,'').replace(/_/g,' ').toLowerCase()),'',''],t=LANG=='tr'&&D.perksTr&&D.perksTr[x],r=t?[t[0]||e[0],e[1],t[1]||e[2]]:e.slice(0,3),
    fl=t&&t[1]?t[2]:e[3];                        // with a fight's own Set values the numbers are filled again
  if(sets&&sets[x]&&fl)r[2]=fillText(fl.t,fl.f,Object.assign({},fl.base,sets[x]));return r}
var XF=[function(x){return x*100},function(x){return x},function(x){return x/60},function(x){return(Math.pow(2,x/1e4)-1)*100},
  function(x){return-x/100},function(x){return 150*Math.pow(2,x/1e4)}];   // builder's PERCENT_TRANSFORMS
function num(v,d){return String(parseFloat(d!=null?v.toFixed(d):v.toPrecision(6)))}
function fillOne(s,st){                          // as builder's fill_one
  if(s[0]=='c')return num(s[1]);if(s[0]=='t')return s[1] in st?num(XF[s[2]](+st[s[1]]),1):'…';
  var x=s[1].replace(/_(\w+)/g,function(m,k){return k in st?st[k]:'?'});
  return/^[\d.\s+\-*\/()]+$/.test(x)?num(Function('return '+x)(),2):'…'}
function fillText(t,f,st){return t.replace(/\{(\d+)\}/g,function(m,i){return f[i]?fillOne(f[i],st):'…'}).replace(/%%/g,'%')}
function abilityName(k){var n=LANG=='tr'&&D.superPerksTr&&D.superPerksTr[k]||(D.abilities[k]||[k])[0];return n.indexOf('PERK_')==0?n.slice(5).replace(/_/g,' '):n}
function abilitySelect(i,c,p){                   // Super: only the character's own abilities (from its forms)
  var own=[];(c.forms||[]).forEach(function(f){f.ability&&own.indexOf(f.ability)<0&&own.push(f.ability)});
  if(!own.length)return'';
  var name=abilityName;   // no text: show the key
  return'Super <select data-super="'+i+'"><option value="">'+T('none')+'</option>'+own.map(function(k){
    return'<option value="'+k+'"'+(p.super==k?' selected':'')+'>'+esc(cap(name(k)))+'</option>'}).join('')+'</select>'}
function enchSelect(i,p){                       // own fighter: one enchantment per slot, on whatever item the dojo puts there
  var e=p.ench||{};
  return'<div class="row ench">'+T('Enchantments')+': '+SLOTS.map(function(t){
    return'<span>'+T(t)+' <select data-ench="'+i+':'+t+'"><option value="">'+T('none')+'</option>'+(D.enchantments[t]||[]).map(function(k){
      return'<option value="'+k+'"'+(e[t]==k?' selected':'')+'>'+esc(PK(k)[0])+'</option>'}).join('')+'</select></span>'}).join(' ')+'</div>'}
function screenFree(){
  var weapons=[],q=S.q.toLocaleLowerCase(LANG);
  D.characters.forEach(function(c){c.kind=='fighter'&&c.weapon&&weapons.indexOf(c.weapon)<0&&weapons.push(c.weapon)});weapons.sort();
  var cards=D.characters.filter(function(c){
    if(q){var h=c.display+' '+c.displayTr+' '+c.weapon+' '+c.name;return(h.toLocaleLowerCase(LANG)+' '+h.toLowerCase()).indexOf(q)>=0}   // matches Turkish İ/ı and plain i
    return c.kind==S.tab&&(!S.weapon||S.tab!='fighter'||c.weapon==S.weapon)}).map(function(c){
    var on=function(i){return mine(i)&&!S.p[i].dojo&&S.p[i].tpl==c.name};   // only the players this screen picks
    var k=on(0)?' p0':on(1)?' p1':'';
    return'<div class="ch'+k+'" data-ch="'+esc(c.name)+'">'+(k?'<span class="tag">'+(k==' p0'?'P1':'P2')+'</span>':'')+avatar(c)+
      esc(cap(dname(c)))+'<div class="w">'+esc(c.weapon)+'</div></div>'}).join('');
  var g=S.setup;
  return bar(T(g?'Your fighter':'Free fight'),1)+'<div class="wrap">'+(S.wait?'<div class="row"><b>'+esc(S.wait)+'</b></div>':'')+
    '<div class="two">'+player(0)+player(1)+'</div>'+(g?'<div class="row muted">'+T('The host chose:')+' '+esc(cap(g.location))+', '+g.rounds+
    ' '+T('rounds')+' &times; '+g.round_time+' s. '+((g.rules||[]).length?T('Fight rules')+': '+esc((g.rules||[]).map(function(x){var m=/Alias="([^"{]+)/.exec(x);return m?ruleName(m[1]):'?'}).join(', '))+'. ':'')+T('You play on the right.')+'</div>':
    '<div class="row">'+T('Place')+' <select data-k="location">'+D.locations.map(function(l){return'<option value="'+l+'"'+(S.location==l?' selected':'')+'>'+esc(cap(l))+'</option>'}).join('')+'</select>'+
    ' '+T('Rounds')+' <input type="number" min="1" max="9" style="width:60px" data-k="rounds" value="'+S.rounds+'">'+
    ' '+T('Seconds')+' <input type="number" min="10" max="999" style="width:70px" data-k="time" value="'+S.time+'">'+
    (S.mode=='single'?' '+T('Difficulty')+' '+diffSelect():'')+'</div>'+rulesBox())+
    '<div class="err">'+esc(err)+'</div>'+
    '<div class="row">'+['boss','underworld','character','fighter'].filter(function(t){return t!='underworld'||D.characters.some(function(c){return c.kind==t})}).map(function(t){return'<button data-tab="'+t+'"'+(S.tab==t?' class="on"':'')+'>'+T({boss:'Bosses',underworld:'Underworld',character:'Characters',fighter:'Fighters'}[t])+'</button>'}).join('')+
    (S.tab=='fighter'?'<select data-k="weapon"><option value="">'+T('All weapons')+'</option>'+weapons.map(function(w){return'<option'+(S.weapon==w?' selected':'')+'>'+w+'</option>'}).join('')+'</select>':'')+
    '<input data-k="q" placeholder="'+T('search')+'" value="'+esc(S.q)+'"><button data-a="random">'+T('Random')+'</button>'+
    '<span class="muted">'+T('Tap a card for the highlighted player.')+'</span></div><div class="grid">'+cards+'</div></div>'}

function screenJson(){
  return bar('match.json',1)+'<div class="wrap"><div class="muted">'+T('What the menu made. Change anything (see the config guide) and start.')+'</div>'+
    '<div class="err">'+esc(err)+'</div><textarea id="sf2json">'+esc(JSON.stringify(S.json||config(),null,2))+'</textarea>'+
    '<div class="row"><button class="go" data-a="startjson">'+T('Fight with this JSON')+'</button></div></div>'}

function draw(){
  var h=S.view=='json'?screenJson():!S.mode?screenMode():S.setup?screenFree():S.mode=='lan'&&!S.host?screenNet():!S.type?screenType():S.type=='story'?screenStory():screenFree();
  var y=body&&body.scrollTop;root.innerHTML=h+'<div class="by">by Sems</div>';body=root;y&&(root.scrollTop=y)}

function on(ev){
  if(ev.target.closest('input,select,label,details'))return;   // their change event handles it (a redraw here would lose it)
  var t=ev.target.closest('[data-a],[data-mode],[data-net],[data-type],[data-fight],[data-ch],[data-player],[data-tab],[data-try]');if(!t)return;
  var a=t.dataset;
  if(a.a=='back'){
    if(S.setup||S.joining){S.ws&&S.ws.close();S.ws=S.setup=S.joining=S.wait=S.type=null;err='';return draw()}   // the other player leaves the lobby
    if(S.wait){S.hws&&S.hws.close();S.hws=S.wait=null;return draw()}   // the host stops waiting: back to its match screen
    if(S.inGame&&!S.mode&&S.view!='json')return closeMenu();   // over the game: back to the dojo
    S.view=='json'?S.view=null:S.type?S.type=null:S.host?(unhost(),S.host=false):S.mode=null;err=''}
  else if(a.net=='join'){try{localStorage.setItem('sf2-relay',S.relay)}catch(e){}return join()}
  else if(a.net=='host')return host();
  else if(a.net=='share')return share();
  else if(a.a=='copyurl')return copy(S.shareUrl);
  else if(a.a=='pick')return pick();
  else if(a.try!=null){var q=S.p[+a.try],c=CH[q.tpl]||{},L=load(q),own=c.items||[];   // the form, slot by slot
    var names=SLOTS.map(function(t,k){return k?L[t]||{Ranged:'NoRanged',Magic:'NoMagic',Helm:'Head',Armor:'Body'}[t]:own[0]||'Fists'});
    return look({items:names,voice:c.voice,perks:(q.super?[q.super]:[]).concat(form(q).traits||[])})}
  else if(a.a=='normal'||a.a=='cheat'){location.href='normal.html'+(a.a=='cheat'?'?cheat':'');return}
  else if(a.a=='copy')return copy(S.hostAddr);
  else if(a.a=='start'){S.type=='story'&&!S.fight?(err=T('Pick a fight from the list.')):start();return draw()}
  else if(a.a=='json'){S.json=null;S.view='json'}
  else if(a.a=='startjson'){return start(document.getElementById('sf2json').value)}
  else if(a.a=='random'){var l=D.characters;pickChar(S.p[S.active],l[Math.random()*l.length|0].name)}
  else if(a.mode){S.mode=a.mode;S.mode=='single'&&(S.active=0)}
  else if(a.type)S.type=a.type;
  else if(a.fight){S.fight=a.fight;err=''}
  else if(a.ch){pickChar(S.p[S.active],a.ch);S.p[S.active].dojo=false;S.mode!='lan'&&(S.active=S.active?0:1)}
  else if(a.tab){S.tab=a.tab;S.q=''}
  else if(a.player!=null&&!ev.target.closest('select,label')&&mine(+a.player))S.active=+a.player;
  draw()}
function change(ev){
  var t=ev.target,a=t.dataset;
  if(a.k){S[a.k]=t.type=='checkbox'?t.checked:t.value}
  else if(a.super!=null)S.p[+a.super].super=t.value;
  else if(a.ench){var q=a.ench.split(':'),pp=S.p[+q[0]];pp.ench=pp.ench||{};pp.ench[q[1]]=t.value}
  else if(a.form!=null){var q=S.p[+a.form],f=(CH[q.tpl].forms||[])[+t.value];q.form=+t.value;q.super=f&&f.ability||''}
  else if(a.tactic)S.p[1].tactic=t.value;
  else if(a.dojo!=null)S.p[+a.dojo].dojo=t.checked;
  else if(a.edit!=null)S.p[+a.edit].edit=t.checked;
  else if(a.ownname){ownSet('name',t.value.trim());return}   // typing: kept, no redraw
  else if(a.ownav!=null)ownSet('avatar',t.value);
  else if(a.extra!=null)S.p[+a.extra].extra=t.value;
  else if(a.rule){var R=D.rules,r=R[a.rule];S.rules=S.rules.filter(function(k){return k!=a.rule&&!(t.checked&&(r.area&&R[k].area||r.win&&R[k].win))});t.checked&&S.rules.push(a.rule)}
  else return;
  if(t.type=='number')return;                    // a number changes nothing else: no redraw (it would take the focus)
  if(t.tagName=='INPUT'&&t.type!='checkbox'&&t.type!='number'){clearTimeout(change.t);change.t=setTimeout(function(){draw();var n=root.querySelector('[data-k="'+a.k+'"]');n&&(n.focus(),n.setSelectionRange(n.value.length,n.value.length))},250)}
  else draw()}

// Try: the dojo shows the form and the menu hides. Its button, Escape, the browser's back and Android's
// back (__sf2Back) all return. Before a match the game starts here only for this.
var bootGame=null,looking=false;
function look(it){                               // it: {items, voice, perks}
  looking=true;closeMenu();history.pushState({sf2look:1},'');
  var b=document.getElementById('sf2back');
  if(!b){b=document.createElement('button');b.id='sf2back';b.onclick=function(){back()};document.body.appendChild(b)}
  b.textContent='\u2190 '+T('Back');b.style.display='';
  window.__sf2LookPending=it;
  if(bootGame){var g=bootGame;bootGame=null;window.__SF2_LOOKBOOT=true;   // the game is not running yet
    window.__SF2_CONFIG={left:{human:true,dojo:true},right:{human:false}};window.__SF2_NETINFO=null;g()}
  else window.__sf2LookAt&&__sf2LookAt(it)}
function back(pop){
  if(!looking)return false;looking=false;
  document.getElementById('sf2back').style.display='none';
  window.__sf2LookPending=null;window.__sf2LookAt&&__sf2LookAt(null);
  pop||history.state&&history.state.sf2look&&history.back();
  openMenu(S.inGame);return true}
window.__sf2Back=function(){return back()};
addEventListener('popstate',function(){back(true)});
addEventListener('keydown',function(e){e.key=='Escape'&&looking&&(e.stopPropagation(),back())},true);
window.__sf2Boot=function(game){
  var q=new URLSearchParams(location.search),saved=sessionStorage.getItem('sf2-config');
  q.has('relay')&&sessionStorage.setItem('sf2-net',JSON.stringify({relay:q.get('relay'),role:q.get('role')||'host'}));
  if(q.has('menu'))sessionStorage.removeItem('sf2-config');
  else{var c=saved?JSON.parse(saved):window.__SF2_CONFIG;   // the menu's match, else server.py's match.json
    if(c){var errs;
      var ni=JSON.parse(sessionStorage.getItem('sf2-net')||'null');errs=__sf2Check(c,!!ni);
      if(!errs.length){window.__SF2_CONFIG=c;window.__SF2_NETINFO=ni;return game()}
      sessionStorage.removeItem('sf2-config');refuse(c,errs)}}
  try{S.relay=localStorage.getItem('sf2-relay')||''}catch(e){}
  REMOTE&&fetch('sf2-netport').then(function(r){return r.json()}).then(function(r){S.relay=location.hostname+':'+r.port},function(){});
  window.SF2Native?S.netPort=SF2Native.netport():
    fetch('sf2-netport').then(function(r){return r.json()}).then(function(r){S.netPort=S.netPort||r.port},function(){});
  restore();bootGame=game;openMenu(false)};   // the game starts with a match, or for Try
function openMenu(inGame){                      // inGame: over the running game (the dojo's MATCH MENU)
  S.inGame=inGame;
  if(!root){var st=document.createElement('style');st.textContent=css;document.head.appendChild(st);
    root=document.createElement('div');root.id='sf2m';root.lang=LANG;   // the language's capitals for CSS uppercase (İKİ, not IKI)
    root.addEventListener('click',on);root.addEventListener('change',change);root.addEventListener('input',change);
    ['keydown','keyup'].forEach(function(k){root.addEventListener(k,function(e){e.stopPropagation()})})}   // typing must not move the fighter
  root.style.display='';window.__sf2Pause&&__sf2Pause(true);   // pause the game behind it
  (document.body?Promise.resolve():new Promise(function(r){addEventListener('DOMContentLoaded',r)})).then(function(){
    root.parentNode||document.body.appendChild(root);draw()})}
window.__sf2OpenMenu=function(){openMenu(true)};
function closeMenu(){root&&(root.style.display='none');window.__sf2Pause&&__sf2Pause(false)}
})();
"""


def missing_locations(src):
    """locations in the file list (G.Bq) without an entry in the fight's preload table (Ef.LAa)"""
    files = re.search(r'G\.Bq="([^"]*)"', src).group(1).split(" ")
    have = set(re.findall(r'case "([^"]+)":return\[', src[src.index("static LAa(a){"):][:20000].split("default:")[0]))
    return sorted({f.split("/")[1] for f in files if f.startswith("locations/") and f.endswith("_params.xml")} - have)


def missing_music(src):
    """music files (audio/<name>_music) without an entry in the music table (va.N0) -> [(name, number)]"""
    files = re.search(r'G\.Bq="([^"]*)"', src).group(1).split(" ")
    i = src.index("static N0(a){"); have = {n.lower() for n in re.findall(r'b\.v\.(\w+)=\s*\d+;', src[i:src.index("va.hHa=b}", i)])}
    return [(f[6:-len("_music.{audio}")], k) for k, f in enumerate(files)
            if f.startswith("audio/") and f.endswith("_music.{audio}") and f[6:-len("_music.{audio}")].lower() not in have]


def location_files(src, name):
    """the numbers of a location's files in the game's file list G.Bq (what the fight scene preloads)"""
    files = re.search(r'G\.Bq="([^"]*)"', src).group(1).split(" ")
    ids = [i for i, f in enumerate(files) if f.startswith(f"locations/{name}/")]
    if not ids:
        sys.exit(f"sf2.js: no files of location {name} in G.Bq (different game version?)")
    return ids


def patch_js(src):
    out = src
    for name, pat, rep in JS_PATCHES:
        n = len(re.findall(pat, out, flags=re.S))
        if n != 1:
            sys.exit(f"sf2.js patch '{name}': pattern found {n} times, expected 1 "
                     "(give the ORIGINAL www folder, not a built one; or a different game version)")
        if callable(rep) and rep.__code__.co_argcount == 2:
            rep = functools.partial(rep, src=src)
        out = re.sub(pat, rep if callable(rep) else (lambda m, r=rep: r), out, flags=re.S)
    return JS_HEADER + out



# =============================================================================== xml2.dat
def unpack(data):
    n = int.from_bytes(data[0:2], "little"); p = 2; out = []
    for _ in range(n):
        ln = data[p]; p += 1
        name = data[p:p + ln].decode("utf-8"); p += ln
        dl = int.from_bytes(data[p:p + 3], "little"); p += 3
        out.append([name, data[p:p + dl]]); p += dl
    if p != len(data):
        raise ValueError("pack format mismatch")
    return out


def pack(entries):
    out = bytearray(len(entries).to_bytes(2, "little"))
    for name, blob in entries:
        nb = name.encode("utf-8")
        out += bytes([len(nb)]) + nb + len(blob).to_bytes(3, "little") + blob
    return bytes(out)


def read_dat(path):
    """a .dat file's entries, and whether it was zstd-compressed"""
    raw = open(path, "rb").read()
    z = raw.startswith(ZSTD_MAGIC)
    if z:
        raw = zstandard.ZstdDecompressor().decompressobj().decompress(raw)
    return unpack(raw), z


def load_entries(www):
    c = sorted(glob.glob(os.path.join(www, "res", "xml2.*.dat")))
    if not c:
        sys.exit("res/xml2.*.dat not found - is this the www folder?")
    data = zstandard.ZstdDecompressor().decompressobj().decompress(open(c[0], "rb").read())
    return c[0], unpack(data)


def get_xml(entries, name):
    return next(b for n, b in entries if n == name).decode("utf-8")


def parse_items(entries):
    """every <Item> with a Type in res/list.xml (items without Type are references inside shop offers)"""
    out = []
    for m in re.finditer(r'<Item\b([^>]*)>', get_xml(entries, "res/list.xml")):
        a = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        if "Type" in a:
            out.append(a)
    return out


# typos in the game's texts (the menu shows them)
LANG_FIXES = {"tr": [("Dare aldıktan", "Darbe aldıktan"), ("bloğunu baypass etme", "bloğunu aşma"),
                     ("sihir doldumanı", "sihir dolumunu")]}
ID_FIXES = [("TELEPORTAION", "TELEPORTATION"), ("FREEZ_", "FREEZE_")]   # typos in perk names shown as names


def load_lang(www, code="en"):
    for f in glob.glob(os.path.join(www, "res", "lang", code + ".*.xml")):
        raw = open(f, "rb").read()
        if raw.startswith(ZSTD_MAGIC):
            raw = zstandard.ZstdDecompressor().decompressobj().decompress(raw)
        lang = dict(re.findall(r'<Word Title="([^"]+)">([^<]*)</Word>', raw.decode("utf-8", "replace")))
        # a text can name another one: "%Tournament_TITLE" (the game resolves it when it shows it: Q_a)
        ref = lambda m: lang.get(m.group(1), m.group(0))
        lang = {k: re.sub(r'%(\w+)', ref, v) for k, v in lang.items()}
        for a, z in LANG_FIXES.get(code, []):
            lang = {k: v.replace(a, z) for k, v in lang.items()}
        return lang
    return {}


# =============================================================================== match.json
# a game fight ("fight"): the settings it takes (the menu's __sf2Check refuses the others)
GAME_FIGHT_SETTINGS = {"fight", "eclipse", "difficulty", "language", "lan_input_delay", "left", "right"}
DIFFICULTIES = ("very_easy", "easy", "middle", "hard", "very_hard")   # the game's diff0..diff4
GAME_FIGHT_SIDE = {"left": {"human", "tactic", "items"}, "right": {"human"}}



ITEM_GROUPS = {"weapons": "Weapon", "ranged": "Ranged", "magic": "Magic", "helms": "Helm",
               "armors": "Armor", "skeletons": "Skeleton"}
DAMAGE_ATTR = {"Weapon": "WeaponDamage", "Ranged": "RangedDamage", "Magic": "MagicDamage",
               "Helm": "HeadDefense", "Armor": "BodyDefense"}


def parse_battles(entries):
    """every <Fight> of the game's map (stages.xml): its id ZONE/BATTLE/FIGHT and what --list shows"""
    out = []
    for zone in ET.fromstring(get_xml(entries, "res/stages.xml")).iter("Zone"):
        if zone.get("Name") == MATCH["zone"]:
            continue
        for bt in zone.iter("Battle"):
            for f in bt.findall("Fight"):
                ws = f.find("Warriors")
                rules = f.find("Rules")
                out.append({
                    "id": f'{zone.get("Name")}/{bt.get("Name")}/{f.get("Name")}',
                    "title": bt.get("Title", ""), "type": bt.get("Type", ""),
                    "location": f.get("Location") or bt.get("Location") or "dojo",   # the game's default (v.hda)
                    "rounds": f.get("Rounds"), "round_time": f.get("RoundTime"),
                    "warriors": [] if ws is None else ws.findall("Warrior"),
                    "rules": [] if rules is None else list(rules),
                    "eclipse": 'Eclipse="' in ET.tostring(f, encoding="unicode"),
                })
    return out


def warrior_label(w):
    if w.find("Groups") is not None:
        return "random (groups)"
    return f'{w.get("Template", w.get("FirstName", "-"))}({w.get("Tactic", "Standard")},{w.get("WarriorPower", "-")})'


def list_data(entries, www):
    """everything server.py --list prints (sf2list.json in the output folder): per list a header and rows;
    battles also carry each fight's match.json and notes"""
    lang = load_lang(www)
    stages = get_xml(entries, "res/stages.xml")
    out = {}
    out["templates"] = {"head": ["TEMPLATE", "NAME", "ITEMS"], "rows": []}
    for m in re.finditer(r'<Template Name="([^"]+)"([^>]*)>(.*?)</Template>', stages, re.S):
        fn = re.search(r'FirstName="([^"]+)"', m.group(2))
        out["templates"]["rows"].append([m.group(1), lang.get(fn.group(1), fn.group(1)) if fn else "",
                                         " ".join(re.findall(r'Item Name="([^"]+)"', m.group(3)))])
    status = item_status(entries, www)
    rows = []
    for a in parse_items(entries):
        t = a["Type"]
        if t not in ITEM_GROUPS.values():
            continue
        dmg = a.get(DAMAGE_ATTR.get(t, ""), "")
        if t == "Armor" and a.get("UnarmedDamage"):
            dmg = f"{a.get('BodyDefense', '0')}/{a['UnarmedDamage']}"
        note = ("hidden" if a.get("Hidden") == "1" else          # the game's internal empty slots
                status[a["Name"]] if t in SLOTS else "")         # skeletons have no dojo slot at all
        rows.append([t, a.get("SubType", ""), a["Name"], a.get("Level", ""), dmg,
                     lang.get(a["Name"], ""), f"({note})" if note else ""])
    out["items"] = {"head": ["TYPE", "SUBTYPE", "NAME", "LVL", "DMG/DEF", "ENGLISH NAME", "NOTE"], "rows": rows}
    out["avatars"] = {"head": None, "rows": [[n] for n in sorted(set(re.findall(r'Avatar="([^"]+)"', stages)))]}
    out["names"] = {"head": None, "rows": [[k, v] for k, v in sorted(lang.items())
                                          if k.startswith("NAME_") or k.startswith("character")]}
    out["perks"] = {"head": None, "rows": [[n, lang.get(n, "")] for n in
                    sorted(set(re.findall(r'<Perk Name="([^"]+)"', get_xml(entries, "res/perks.xml"))))]}
    out["tactics"] = {"head": None, "rows": [[t] for t in
                      re.findall(r'<Tactic Name="[^"]+"[^>]*>', get_xml(entries, "res/tactic_settings.xml"))]}
    for what, attr in (("locations", "Location"), ("music", "Music")):
        out[what] = {"head": None, "rows": [[n] for n in sorted(set(re.findall(attr + r'="([^"]+)"', stages)))]}
    rows, js, notes, details = [], [], [], []
    tpl = {t.get("Name"): t for t in ET.fromstring(stages).iter("Template")}

    def own(w, what):                                # a warrior's perks / items: its template chain's, then its own
        out, t, seen = [], w.get("Template"), set()
        while t in tpl and t not in seen:
            seen.add(t)
            out = [x.get("Name") for x in tpl[t].iter(what) if x.get("Name")] + out
            t = tpl[t].get("Template")
        return out + [x.get("Name") for x in w.iter(what) if x.get("Name")]
    for b in parse_battles(entries):
        ops = b["warriors"]
        opp = (warrior_label(ops[0]) if ops else "-") + (f" +{len(ops) - 1}" if len(ops) > 1 else "")
        rows.append([b["id"], b["type"], b["location"], f'{b["rounds"] or 2}x{b["round_time"] or 60}s', opp,
                     str(len(b["rules"])), lang.get(b["title"], "")])
        js.append({"fight": b["id"]})
        n = []
        if len(ops) > 1:
            n.append(f"{len(ops)} opponents, one after another (a new one each round you win)")
        if any(warrior_label(x) == "random (groups)" for x in ops):
            n.append("an opponent is picked at random from warrior groups, a new one each time")
        n.append('"eclipse": true plays it in the game\'s Eclipse mode (stronger opponents'
                 + (', and this fight\'s Eclipse-only rules)' if b["eclipse"] else ')'))
        if b["type"] == "DUMMY":
            n.append("a dojo training fight (the game plays it inside the dojo): no rounds, no end")
        notes.append(n)
        d = ["rule: " + re.sub(r"\s+", " ", ET.tostring(r, encoding="unicode")).strip() for r in b["rules"]]
        for k, w in enumerate(ops):
            attrs = " ".join(f'{a}={v}' for a, v in w.attrib.items() if a not in ("Template", "Tactic", "WarriorPower"))
            d.append(f"opponent {k + 1}: {warrior_label(w)}" + (f" {attrs}" if attrs else "")
                     + f"\n           items: {' '.join(own(w, 'Item')) or '-'}"
                     + f"\n           perks: {' '.join(own(w, 'Perk')) or '-'}")
        details.append(d)
    out["battles"] = {"head": ["FIGHT", "TYPE", "LOCATION", "ROUNDS", "OPPONENT(TACTIC,POWER)", "RULES", "TITLE"],
                      "rows": rows, "json": js, "notes": notes, "details": details}
    return out


def check_data(entries, www):
    """what the browser checks a match.json against (__SF2DATA.check, __sf2Check in the menu)"""
    stages = get_xml(entries, "res/stages.xml")
    model = {i["Name"]: i.get("Model") for i in parse_items(entries)}
    why = {"no 3D model": "has no 3D model in this game version",
           "not for fighters": "is not for fighters (its model needs nodes a fighter's skeleton does not have)"}
    return dict(
        items={i["Name"]: i["Type"] for i in parse_items(entries)},
        perks=sorted(set(re.findall(r'<Perk Name="([^"]+)"', get_xml(entries, "res/perks.xml")))),
        enchantments=sorted(enchantments(entries)),
        tactics=sorted(set(re.findall(r'<Tactic Name="([^"]+)"', get_xml(entries, "res/tactic_settings.xml")))),
        locations=sorted(set(re.findall(r'Location="([^"]+)"', stages))),
        fights=[b["id"] for b in parse_battles(entries)],
        # an item without Model= (Fists, NoRanged, NoMagic: an empty slot) is fine
        why={n: why[st] for n, st in item_status(entries, www).items()
             if st in why and (st != "no 3D model" or model.get(n))},
        languages=LANGUAGES, slots=SLOTS, gameFight=sorted(GAME_FIGHT_SETTINGS), broken=template_screen(entries, www)[0],
        gameSide={k: sorted(v) for k, v in GAME_FIGHT_SIDE.items()})


def private_famobi(www):
    """the portal config without analytics / login / cloud save (LAN without pause: patch "no pause in
    lockstep")"""
    cfg = json.load(open(os.path.join(www, "famobi.json"), encoding="utf-8"))
    cfg.setdefault("analytics", {})["enabled"] = False
    a = cfg.setdefault("auth", {}); a["enabled"] = False
    a.setdefault("overlay", {})["enabled"] = False
    a.setdefault("cloudSave", {})["enabled"] = False
    cfg.setdefault("features", {})["standalone"] = True
    return json.dumps(cfg).encode("utf-8")


# Quests are the campaign script: story and tutorial dialogs, offers, ads, energy, pack downloads,
# level-up screens. They react to fights and scene changes and would take over the match and the dojo,
# so all of them go, except these, which only navigate the game's UI:
KEEP_QUESTS = (
    # leaves the loading screen: on SessionStart it resumes saved quests; with none it runs a nameless
    # <Fight/>, which ends up as "go to the dojo" (Wn.S: p.ew -> null -> xp(3))
    "ScriptsResumeOnStart",
    # the STORE button of the fight result only fires an event; this quest opens the shop
    "ShopAfterBattleButtonPress",
)


def keep_quests(data):
    out = []
    for name in KEEP_QUESTS:
        q = re.search(rb'<Quest Name="' + name.encode() + rb'".*?</Quest>', data, re.S)
        if not q:
            sys.exit(f"res/quests.xml: quest {name} not found (different game version?)")
        out.append(q.group(0))
    return b"<Root>" + b"".join(out) + b"</Root>"


LANGUAGES = ("en", "de", "es", "fr", "it", "ja", "ko", "pt", "ru", "tr")   # the game's G.R9


SLOTS = ("Weapon", "Ranged", "Magic", "Helm", "Armor")


def enchantments(entries):
    """item enchantment perk -> its slot: an Alias ENCHANTMENT_... up its Template chain, the slot its name ends
    with (PERK_ITEM_SPECIAL_LIFESTEAL_WEAPON -> Weapon)"""
    perks = {m.group(1): dict(re.findall(r'(\w+)="([^"]*)"', m.group(0)))
             for m in re.finditer(r'<Perk Name="([^"]+)"[^>]*>', get_xml(entries, "res/perks.xml"))}
    out = {}
    for n in perks:
        t, seen = n, set()
        while t in perks and t not in seen and not perks[t].get("Alias", "").startswith("ENCHANTMENT_"):
            seen.add(t); t = perks[t].get("Template")
        slot = next((x for x in SLOTS if n.endswith("_" + x.upper())), None)
        if t in perks and slot:
            out[n] = slot
    return out


def aspect(entries, x):
    """the game's Aspect curve (De.zea, internal_settings.xml <Aspect>): an enchantment's power -> its factor"""
    a = ET.fromstring(get_xml(entries, "res/internal_settings.xml").lstrip("\ufeff")).find(".//Aspect")
    lim, anti, dr = float(a.get("Limit")), float(a.get("Antilimit")), float(a.get("DoublingRange"))
    return lim - (lim - 1) * 2 ** (-x / dr) if x >= 0 else anti + 2 ** (x / dr)


def enchant_aspect(entries, level):
    """an enchantment's strength (Set Aspect) at a level: the forge's table (forge.xml AspectScale, as the game's Ae.Bea)"""
    for a in ET.fromstring(get_xml(entries, "res/forge.xml")).find("AspectScale"):
        lo = int(a.get("Level") or a.get("MinLevel") or 0); hi = int(a.get("Level") or a.get("MaxLevel") or 1 << 31)
        if lo <= level <= hi:
            return int(a.get("Value"))
    return 0


def item_picture(image):
    """an item's Image ("Weapon1.img_weapon_sai") as the frame name the item list asks the
    placeholder atlas res/items/proxy for (Tf: "." -> "/", file part lower case, "-" -> "_")"""
    a = image.replace(".", "/").replace("-", "_")
    return a.split("/")[0] + "/" + a.split("/")[1].lower() if "/" in a else a.lower()


def game_models(www):
    """the game's 3D models: res/models.*.dat, the same container as xml2 -> {name: xml text}"""
    c = sorted(glob.glob(os.path.join(www, "res", "models.*.dat")))
    if not c:
        sys.exit("res/models.*.dat not found - is this the full www folder?")
    raw = zstandard.ZstdDecompressor().decompressobj().decompress(open(c[0], "rb").read())
    return {n: d.decode("utf-8", "replace") for n, d in unpack(raw)}


# An item's model hangs on the nodes (bones) of a host model: a "MacroNode" names them in
# ChildNode1..N, and the game throws (bare number 22) when one is missing (EGa). Weapon, helm and armor
# hang on the fighter's skeleton, a ranged item on the flying object (SkeletonMissile), magic on the
# spell object (SkeletonMagic)
ITEM_HOST = {"Weapon": "mdl_skeleton", "Helm": "mdl_skeleton", "Armor": "mdl_skeleton",
             "Ranged": "mdl_skeleton_missile", "Magic": "mdl_skeleton_magic"}


def model_nodes(xml):
    return set(re.findall(r'<([A-Za-z0-9_\-]+) [^>]*Type="', xml))


def fits_host(item, models):
    model, host = models.get(item.get("Model")), models.get(ITEM_HOST.get(item["Type"], ""))
    if not model or not host:
        return True
    return not (set(re.findall(r'ChildNode\d+="([^"]+)"', model)) - model_nodes(model) - model_nodes(host))


_ITEM_STATUS = {}


def item_status(entries, www):
    if www not in _ITEM_STATUS:
        _ITEM_STATUS[www] = _item_status(entries, www)
    return _ITEM_STATUS[www]


def _item_status(entries, www):
    """name -> why the item is not in the dojo inventory ("" = it is):
    "no 3D model"      its Model= is not in res/models.dat (some event items never shipped one; wearing
                       it stops the fighter)
    "not for fighters" its model hangs on nodes the fighter's skeleton lacks (item_host)
    "not in dojo"      no picture in the item atlas res/items/proxy, which the inventory needs for every
                       owned item (boss and enemy heads, bodies, weapons, magic)"""
    atlas = sorted(glob.glob(os.path.join(www, "res", "items", "proxy.*.json")))
    if not atlas:
        sys.exit("res/items/proxy.*.json not found - is this the full www folder?")
    frames = json.load(open(atlas[0], encoding="utf-8"))["frames"]
    drawable = {item_picture(f["filename"] if isinstance(f, dict) else f) for f in frames}
    models = game_models(www)
    out = {}
    for i in parse_items(entries):
        if i.get("Model") not in models:
            out[i["Name"]] = "no 3D model"
        elif not fits_host(i, models):
            out[i["Name"]] = "not for fighters"   # e.g. MAGIC_MINE: the "weapon" of the mine a perk drops
        elif not i.get("Image") or item_picture(i["Image"]) not in drawable:
            out[i["Name"]] = "not in dojo"
        else:
            out[i["Name"]] = ""
    return out


def all_items_profile(entries, www):
    """the game's default new-user save, with every item the dojo inventory can show (item_status)
    owned, max level, tutorial done, upgrades on"""
    xml = get_xml(entries, "res/users_default.xml")
    # ShowUpgrades: the shop's upgrade button (the game turns it on with a quest at level 4, quests are gone)
    for k, v in {"Money": "99999999", "Bonus": "999999", "Level": "52", "Tutorial": "END", "ShowUpgrades": "1"}.items():
        xml = re.sub(r'(<Warrior\b[^>]*?\b' + k + r')="[^"]*"', r'\1="' + v + '"', xml, count=1)
    status = item_status(entries, www)
    have = set(re.findall(r'<Item Name="([^"]+)"', xml))
    # items without a level of their own (boss weapons: "adapts to your level") come as the game gives
    # them, at the player's level (reward Level="?Player[].Level"): upgrade 100 x 52. Otherwise their value
    # would be the catalog's placeholder (WeaponDamage -4)
    level0 = set(re.findall(r'<Item Name="([^"]+)"(?![^>]*UpgradeLevel=)[^>]*>\s*<Upgrades ',
                            get_xml(entries, "res/list.xml")))
    extra = "".join(f'<Item Name={quoteattr(i["Name"])} Count="1"'
                    + (' UpgradeLevel="5200"' if i["Name"] in level0 else "") + "/>"
                    for i in parse_items(entries)
                    if i["Type"] in SLOTS and i["Name"] not in have and status[i["Name"]] == "")
    return xml.replace("</Items>", extra + "</Items>", 1).encode("utf-8")


def off_label(move):
    """the game loads a move with Labels only if they name PAID (Fa.sdb; this build is the paid game)"""
    m = re.match(r'<Move\b[^>]*\bLabels="([^"]*)"', move)
    return bool(m) and "PAID" not in m.group(1).split("|")


def ability_perks(entries):
    """perks that give a fighter a move of its own, with the key a player starts it with: "Super",
    "Ability" (no button here) or "Up" (boss abilities like PERK_HERMITSTORM). The AI ignores keys; it
    starts the move when the move's conditions hold. Only perks perks.xml defines (the game drops unknown
    ones such as the console's *_PVP perks), and no move locked to a skeleton the item list lacks
    (Director Hoaxen's BODY_TENTACLE_SKELETON exists; the menu puts it on with the Super)"""
    keys, worn = {}, set(re.findall(r'<Item Name="([^"]+)"', get_xml(entries, "res/list.xml")))
    for m in ET.fromstring(get_xml(entries, "res/moves.xml")).iter("Move"):
        lk = m.find("Locks")
        if lk is None or off_label(ET.tostring(m, encoding="unicode")):
            continue
        if {i.get("Name") for i in lk.iter("Item") if i.get("Type") == "Skeleton" and i.get("Name")} - worn:
            continue
        k = {x.get("Type") for x in m.iter("Key")}
        for x in lk.iter("Perk"):
            keys.setdefault(x.get("Name"), set()).update(k)
    defined = set(re.findall(r'<Perk Name="([^"]+)"', get_xml(entries, "res/perks.xml")))
    out = {}
    for p, k in keys.items():
        if p in defined:
            key = "Super" if "Super" in k else "Ability" if "Ability" in k else "Up" if k == {"Up"} else None
            key and out.setdefault(p, key)
    return out


def template_screen(entries, www):
    """(hidden templates, picture stand-ins), by rules, nothing named:
    hidden: a template wearing an item whose Model= is not in res/models.dat (the fight never finishes
      loading), or a copy that only changes looks or name (Avatar, FirstName, Voice).
    stand-in: a kept template whose picture is not in res/users/images (the fight's HUD stopped on it)
      shows Default's picture, the classic shadow.
    This version: Man_XMAS14 and the five Ascension_2 ninjas are hidden; 11 old characters (Girl_Whip,
    Man_Katana ...) get the stand-in. The game's own fights use none of them."""
    root = ET.fromstring(get_xml(entries, "res/stages.xml"))
    tpl = {t.get("Name"): t for t in root.iter("Template")}
    pics = {os.path.basename(f).split(".")[0] for f in glob.glob(os.path.join(www, "res", "users", "images", "*.webp"))}
    models = game_models(www)
    model = {i["Name"]: i.get("Model") for i in parse_items(entries)}
    looks = {"Name", "Template", "Avatar", "FirstName", "Voice"}

    def chain(n):
        out = []
        while n in tpl and n not in out:
            out.append(n); n = tpl[n].get("Template")
        return out

    def avatar(n):
        return next((tpl[x].get("Avatar") for x in chain(n) if tpl[x].get("Avatar")), None)
    hidden, fix = [], {}
    for n, t in tpl.items():
        broken = any(model.get(it.get("Name")) and model[it.get("Name")] not in models
                     for x in chain(n) for it in tpl[x].iter("Item"))
        copy = t.get("Template") in tpl and not list(t) and set(t.attrib) <= looks
        if broken or copy:
            hidden.append(n)
        elif avatar(n) not in pics:
            fix[n] = avatar("Default")
    return sorted(hidden), fix


def template_items(entries, name):
    """the items a stages.xml <Template> gives, its parent templates' included (a child replaces per slot)"""
    tpl = {t.get("Name"): t for t in ET.fromstring(get_xml(entries, "res/stages.xml")).iter("Template")}
    types = {i["Name"]: i["Type"] for i in parse_items(entries)}
    chain = []
    while name in tpl and name not in chain:
        chain.append(name); name = tpl[name].get("Template")
    out = {}
    for n in reversed(chain):
        for it in tpl[n].iter("Item"):
            if types.get(it.get("Name")) in SLOTS:
                out[types[it.get("Name")]] = it.get("Name")
    return out


def probe_gear(entries, www):
    """per slot of SLOTS: [[item, level], ...] by level - plain items (an upgrade table, no enchantment
    perks) the dojo can show; their value depends on the upgrade only, so one per level is enough"""
    lst = get_xml(entries, "res/list.xml")
    status = item_status(entries, www)
    out = []
    for t in SLOTS:
        by_level = {}
        for m in re.finditer(r'<Item ([^>]*Type="' + t + r'"[^>]*)>(.*?)</Item>', lst, re.S):
            a = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
            if status.get(a.get("Name")) == "" and a.get("Level") and "<Upgrades" in m.group(2) \
                    and "<Enchantments" not in m.group(2):
                by_level.setdefault(int(a["Level"]), a["Name"])
        out.append([[n, lv] for lv, n in sorted(by_level.items())])
    return out


def super_mods(entries):
    """the mods (perks.xml FlagName) a Super perk keeps on the fighter while its move recharges"""
    perks = {p.get("Name"): p for p in ET.fromstring(get_xml(entries, "res/perks.xml")).iter("Perk")}
    out = set()
    for name in super_perks(entries):
        seen = set()
        while name in perks and name not in seen:
            seen.add(name)
            st = perks[name].find("Set")
            if st is not None and st.get("FlagName"):
                out.add(st.get("FlagName")); break
            name = perks[name].get("Template")
    return sorted(out)


def ability_skeleton(entries):
    """an ability whose move locks a skeleton of its own (Director Hoaxen: BODY_TENTACLE_SKELETON, the
    skeleton with tentacles; worn by no fighter of the game) -> that skeleton: the menu puts it on"""
    out = {}
    for m in ET.fromstring(get_xml(entries, "res/moves.xml")).iter("Move"):
        lk = m.find("Locks")
        if lk is None or off_label(ET.tostring(m, encoding="unicode")):
            continue
        if not any(k.get("Type") in ("Super", "Up") for k in m.iter("Key")):   # the fighter's own move only
            continue
        sk = [i.get("Name") for i in lk.iter("Item") if i.get("Type") == "Skeleton" and i.get("Name")]
        for x in lk.iter("Perk"):
            sk and out.setdefault(x.get("Name"), sk[0])
    return {p: k for p, k in out.items() if p in ability_perks(entries)}


def super_perks(entries):
    """perks the Super button starts (Q, control 14): an ability on the Super key, or on Up while charged
    (Hermit's storm, Arkhos' rat wave: the button starts them too, tested)"""
    return sorted(p for p, k in ability_perks(entries).items() if k in ("Super", "Up"))


def item_sets(entries):
    """list.xml ItemSets: set bonus perk (DefaultComboPerk) -> the items of the set. The web engine reads
    ItemSet but never gives its bonus (Rt: DefaultComboPerk read and dropped); __sf2Sets gives it"""
    out = {}
    for st in ET.fromstring(get_xml(entries, "res/list.xml")).iter("ItemSet"):
        if st.get("DefaultComboPerk"):
            out[st.get("DefaultComboPerk")] = [i.get("Name") for i in st.iter("Item")]
    return out


def start_equipment(entries):
    """what the new player wears: Weapon="Fists", Helm="Head" ... on <Warrior> of users_default.xml"""
    w = re.search(r"<Warrior\b[^>]*>", get_xml(entries, "res/users_default.xml")).group(0)
    return [re.search(r'\b' + t + r'="([^"]*)"', w).group(1) for t in SLOTS]


def home_screen_app(www):
    """iPhone Safari has no fullscreen for pages; the address bar goes away only when the page is added
    to the home screen. These tags and sf2.webmanifest make it open as a full-screen app."""
    html = open(os.path.join(www, "index.html"), encoding="utf-8").read()
    tags = ('<meta name="apple-mobile-web-app-capable" content="yes">'
            '<meta name="mobile-web-app-capable" content="yes">'
            '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">')
    tags += '<link rel="manifest" href="sf2.webmanifest">'
    html = html.replace('<meta charset="utf-8">', '<meta charset="utf-8">' + tags, 1)
    # the build's data, the match (match.js: window.__SF2_CONFIG) and the menu load before the game; the
    # game starts when the menu is done (__sf2Boot; at once when a match is set)
    html = html.replace("<script>", '<script src="sf2data.js"></script><script src="match.js"></script>'
                        '<script src="sf2menu/menu.js"></script><script>', 1)
    html = html.replace("window.GameInterface.init(", "function __sf2Game(){window.GameInterface.init(", 1)
    html = html.replace(".then(() => SF2.main());", ".then(() => SF2.main())}__sf2Boot(__sf2Game);", 1)
    if "__sf2Boot(__sf2Game)" not in html:
        sys.exit("index.html: the game's start (GameInterface.init) not found (different game version?)")
    html = html.replace("width=device-width,", "width=device-width, viewport-fit=cover,", 1)
    return html.encode("utf-8")


def build(www, out, patched):
    """out = copy of www (unchanged files copied only once) + the patched files"""
    for root, _, files in os.walk(www):
        rel_root = os.path.relpath(root, www)
        os.makedirs(os.path.join(out, rel_root), exist_ok=True)
        for f in files:
            rel = os.path.normpath(os.path.join(rel_root, f))
            if rel in patched:
                continue
            src, dst = os.path.join(www, rel), os.path.join(out, rel)
            st = os.stat(src)
            if os.path.exists(dst) and os.path.getsize(dst) == st.st_size and os.path.getmtime(dst) >= st.st_mtime:
                continue
            shutil.copy2(src, dst)
    for rel, data in patched.items():
        os.makedirs(os.path.dirname(os.path.join(out, rel)), exist_ok=True)
        with open(os.path.join(out, rel), "wb") as fh:
            fh.write(data)


def runtime_data(entries, www, profile):
    """what the browser needs to set up a match (__SF2DATA): defaults, item types, templates ..."""
    status = item_status(entries, www)
    items = parse_items(entries)
    types = {i["Name"]: i["Type"] for i in items if i["Type"] in SLOTS}
    templates = {}
    for t in ET.fromstring(get_xml(entries, "res/stages.xml")).iter("Template"):
        own = {types[it.get("Name")]: it.get("Name") for it in t.iter("Item") if it.get("Name") in types}
        templates[t.get("Name")] = [t.get("Template"), own]
    return dict(
        match=MATCH, defaults=DEFAULTS, difficulties=DIFFICULTIES, keys=KEYS,
        packs=list(dict.fromkeys(re.findall(r'<Pack Name="([^"]+)"', get_xml(entries, "res/packs.xml")))),
        start=start_equipment(entries), types=types, dojo=sorted(n for n in types if status.get(n) == ""),
        templates=templates, superPerks=super_perks(entries), superMods=super_mods(entries),
        probeGear=probe_gear(entries, www), maxLevel=MAX_LEVEL, check=check_data(entries, www),
        enchAspect=enchant_aspect(entries, MAX_LEVEL),
        itemSets=item_sets(entries),
        avatarFix=template_screen(entries, www)[1],
        profile=hashlib.sha1(profile).hexdigest()[:12])


PLAYER_TEMPLATE = "Default"   # the player's template (custom matches): a story character the player becomes
MAX_LEVEL = 52         # the player's top level: free fights are fought at it (enchantments' strength too)
GENERIC_NAMES = 3      # a FirstName more templates share is a generic fighter ("Ninja"), not a character


def body_bound(entries, www):
    """Super abilities whose animation moves more nodes than the start outfit's models have (skeleton,
    armor, helm; the game reads model node n for animation node n). Android animations store the node
    count in the header; models are XML with one <Nodes> element per node"""
    files = {}
    for kind in ("models", "animations"):
        for p in glob.glob(os.path.join(www, "res", kind + "*.dat")):
            files.update(dict(read_dat(p)[0]))
    lx = get_xml(entries, "res/list.xml")
    model = dict(re.findall(r'<Item Name="([^"]+)"[^>]*?Model="([^"]+)"', lx))
    w = re.search(r"<Warrior\b[^>]*>", get_xml(entries, "res/users_default.xml")).group(0)

    def nodes(item):
        d = files.get(model.get(item))
        n = d and ET.fromstring(d.decode("utf-8", "replace").lstrip("\ufeff")).find("Nodes")
        return len(n) if n is not None else 0
    plain = sum(nodes(re.search(r'\b%s="([^"]*)"' % k, w).group(1)) for k in ("Skeleton", "Armor", "Helm"))
    out = set()
    for m in re.finditer(r'<Move Name="[^"]+"[^>]*FileName="([^"]+)"[^>]*>.*?</Move>', get_xml(entries, "res/moves.xml"), re.S):
        lk = re.search(r'<Locks>(.*?)</Locks>', m.group(0), re.S)
        a = files.get(m.group(1).rsplit(".", 1)[0])
        if lk and 'Key Type="Super"' in m.group(0) and a and a[:1] == b"\0" and int.from_bytes(a[6:8], "little") > plain:
            out |= set(re.findall(r'<Perk Name="([^"]+)"', lk.group(1)))
    return out


def menu_data(entries, www, out):
    """what the menu shows: fights, characters (templates) with their items, items with pictures
    (converted from the game's DXT5 textures to WebP in sf2menu/items), locations"""
    from PIL import Image
    lang = load_lang(www)
    tr_lang = load_lang(www, "tr")
    root = ET.fromstring(get_xml(entries, "res/stages.xml"))
    tpl = {t.get("Name"): t for t in root.iter("Template")}

    def inherited(n, k):
        seen = set()
        while n in tpl and n not in seen:
            seen.add(n)
            if tpl[n].get(k) is not None:
                return tpl[n].get(k)
            n = tpl[n].get("Template")

    items = {i["Name"]: i for i in parse_items(entries)}
    status = item_status(entries, www)
    broken, fix = template_screen(entries, www)
    abil = ability_perks(entries)
    ench = enchantments(entries)
    # Every appearance of a template in the game's fights (story order) with its gear and abilities there.
    # A story character without a template of its own is a named warrior on a generic one (Sensei's
    # memories: Spark, Cleric ... on Guard_Girl / Guard_Man), or the player made into one by a fight's rules
    # (Name, Avatar, EquipItem: Sensei). Each becomes a character (story: (template, name)).
    tnames = {}                                      # a name shown -> the templates that have it
    for t in tpl:
        tnames.setdefault(lang.get(inherited(t, "FirstName"), inherited(t, "FirstName")), []).append(t)
    shown = lambda fn: lang.get(fn, fn)
    apps, story, savatar = {}, {}, {}
    for z in root.iter("Zone"):
        if z.get("Name") == "ZONE_T":
            continue
        for bt in z.iter("Battle"):
            for f in bt.findall("Fight"):
                where = f"{z.get('Name')}/{bt.get('Name')}/{f.get('Name')}"
                ws = f.find("Warriors")
                ws = ws.findall("Warrior") if ws is not None else []
                for k, w in enumerate(ws):               # several: one after another (phases) -> "#k"
                    t, fn = w.get("Template"), w.get("FirstName")
                    if not t:
                        continue
                    app = (bt.get("Type"), w, where + (f"#{k + 1}" if len(ws) > 1 else ""))
                    if fn and shown(fn) != shown(inherited(t, "FirstName")) and shown(fn) not in tnames:
                        story.setdefault((t, fn), []).append(app); savatar[(t, fn)] = w.get("Avatar")
                    elif fn and shown(fn) != shown(inherited(t, "FirstName")) and len(tnames[shown(fn)]) == 1:
                        apps.setdefault(tnames[shown(fn)][0], []).append(app)   # a character the game has: its form
                    else:
                        apps.setdefault(t, []).append(app)
                ru = f.find("Rules")
                nm, av = (ru.find("Name"), ru.find("Avatar")) if ru is not None else (None, None)
                if nm is not None and av is not None:    # the player as someone else: his gear by the rules
                    w = ET.Element("Warrior"); its = ET.SubElement(w, "Items")
                    for e in ru.findall("EquipItem"):
                        ET.SubElement(its, "Item", Name=e.get("Name"))
                    key = (PLAYER_TEMPLATE, nm.get("Name"))
                    story.setdefault(key, []).append((bt.get("Type"), w, where)); savatar[key] = av.get("Name")
    wearable = {n for n, st in status.items() if st in ("", "not in dojo")}
    names = {}
    for n in tpl:
        names.setdefault(inherited(n, "FirstName"), []).append(n)
    chars = []
    for key in list(tpl) + list(story):
        n = key if isinstance(key, str) else key[0]
        if n in broken:
            continue
        fn, av = inherited(n, "FirstName"), inherited(n, "Avatar") or ""
        if not isinstance(key, str):
            fn, av = key[1], savatar[key] or av
        own = template_items(entries, n)
        mine = apps.get(n, []) if isinstance(key, str) else story[key]
        if not isinstance(key, str):                 # a story character: the weapon it fights with there
            wpn = [i.get("Name") for _, x, _ in mine for i in x.iter("Item") if (items.get(i.get("Name")) or {}).get("Type") == "Weapon"]
            own = {**own, "Weapon": wpn[-1]} if wpn else own
        w = items.get(own.get("Weapon"), {})
        kind = ("underworld" if any(x[2].startswith("ZONE_RAID/") for x in mine) else
                "character" if not isinstance(key, str) else
                "boss" if av.startswith("boss") else
                "fighter" if len(names[fn]) >= GENERIC_NAMES else "character")
        # forms: the character as it is in the game, its template and each different way it appears in the
        # game's fights (ranged, magic, helm, armor, ability and item perks together, never mixed; not the fight's
        # attributes: a free fight is at equal strength). Default: a boss's last phase of its own fight, others' last fight
        empty = {"NoRanged", "NoMagic"}                    # an empty slot
        asp52 = enchant_aspect(entries, MAX_LEVEL)

        def form(label, w):
            load = {t: own.get(t) if own.get(t) not in empty else None for t in SLOTS[1:]}
            perks, sets = [], {}
            if w is not None:
                for it in w.iter("Item"):
                    t = (items.get(it.get("Name")) or {}).get("Type")
                    if t in load and (it.get("Name") in wearable or it.get("Name") in empty):
                        load[t] = it.get("Name") if it.get("Name") not in empty else None
                perks = [x.get("Name") for x in w.iter("Perk")]   # the game gives a warrior its enchantments as perks
                sets = {x.get("Name"): dict(x.find("Set").attrib) for x in w.iter("Perk") if x.find("Set") is not None}
            return {"label": label, "load": load, "ability": next((q for q in perks if q in abil), None),
                    "traits": [q for q in perks if q not in abil and q not in ench and q != "PERK_BEGINNER"],
                    "ench": [q for q in perks if q in ench],
                    # the traits' values in that fight; an item special's strength (Aspect) at the top level, like the
                    # enchantments' (a free fight at equal strength)
                    "sets": {q: {**v, **({"Aspect": str(asp52)} if "Aspect" in v else {})} for q, v in sets.items()
                             if q in perks and q not in abil and q not in ench and q != "PERK_BEGINNER"}}
        forms, index, dflt = ([form("template", None)] if isinstance(key, str) else []), {}, 0
        for ty, x, where in mine:
            f = form(where, x)
            fkey = json.dumps([f["load"], f["ability"], f["traits"], f["sets"]], sort_keys=True)
            if fkey not in index:
                index[fkey] = len(forms); forms.append(f)
            if kind != "boss" or ty == "BOSSES":           # the default: the last one of these
                dflt = index[fkey]
        chars.append({**({} if isinstance(key, str) else {      # a template the game lacks (Guard_Man: its warriors
                          "template": n if n in tpl else PLAYER_TEMPLATE,   # stand alone) -> the player's
                          "fname": fn, "weaponItem": own.get("Weapon")}),
                      "name": n if isinstance(key, str) else f"{n}~{fn}",
                      "display": lang.get(fn, fn or n), "displayTr": tr_lang.get(fn) or lang.get(fn, fn or n), "avatar": fix.get(n, av) if isinstance(key, str) else av, "kind": kind,
                      "voice": inherited(n, "Voice") or "",
                      "weapon": re.sub(r"(?<=[a-z])(?=[A-Z])", " ", w.get("SubType", "")),   # TitanGiantSword -> Titan Giant Sword
                      "items": [own.get(t) for t in SLOTS],
                      "forms": forms, "form": dflt})
    used = ({i for c in chars for i in c["items"] if i} | {i for c in chars for f in c["forms"] for i in f["load"].values() if i}
            | {n for n, st in status.items() if st == ""})
    pics = os.path.join(out, "sf2menu", "items")
    os.makedirs(pics, exist_ok=True)
    res = os.path.join(www, "res", "items", "images-1x")
    item_info = {}
    for n in sorted(used):
        it = items.get(n)
        if not it or it["Type"] not in SLOTS:
            continue
        img = None
        if it.get("Image"):
            folder, _, file = it["Image"].partition(".")
            found = glob.glob(os.path.join(res, folder.lower(), file.lower().replace("-", "_") + ".*.dds"))
            dst = os.path.join(pics, n + ".webp")
            if found and (os.path.exists(dst) or not Image.open(found[0]).save(dst, "WEBP", quality=85)):
                img = "sf2menu/items/" + n + ".webp"
        item_info[n] = {"type": it["Type"], "display": lang.get(n) or item_title(n), "img": img,
                        "level": int(it.get("Level") or 0), "dojo": status.get(n) == ""}
    avatars = {os.path.basename(f).split(".")[0]: "res/users/images/" + os.path.basename(f)
               for f in glob.glob(os.path.join(www, "res", "users", "images", "*.webp"))}
    music = {}
    for bt in root.iter("Battle"):
        for f in bt.findall("Fight"):
            loc, mus = f.get("Location") or bt.get("Location"), f.get("Music") or bt.get("Music")
            if loc and mus:
                music.setdefault(loc, []).append(mus)
    # the dojo: the game fights there only in training (no music); a place for a match too (patches
    # "dojo as a fight location", "dojo location kept for a fight in the dojo")
    music.setdefault("dojo", [DEFAULTS["music"]])
    fights = []
    for b in parse_battles(entries):
        fights.append({"id": b["id"], "type": b["type"], "title": lang.get(b["title"], ""),
                       "location": b["location"], "eclipse": b["eclipse"],
                       "opponents": [lang.get(inherited(w.get("Template"), "FirstName") or w.get("FirstName"),
                                              w.get("FirstName") or w.get("Template") or "?")
                                     if w.find("Groups") is None else "?" for w in b["warriors"]]})   # ?: random from groups
    extra = extra_perks(entries, ench)
    pnames = {t for c in chars for f in c["forms"] for t in f["traits"]} | set(abil) | set(ench) | set(extra)
    return {"characters": chars, "items": item_info, "avatars": avatars, "fights": fights,
            "locations": sorted(music), "music": {k: max(set(v), key=v.count) for k, v in music.items()},
            "tactics": sorted(set(re.findall(r'<Tactic Name="([^"]+)"', get_xml(entries, "res/tactic_settings.xml")))),
            "superPerks": {n: lang.get(n, n) for n in super_perks(entries)},
            "abilities": {n: [lang.get(n, n), k] for n, k in abil.items()},
            # a Super whose animation moves more model nodes than the start outfit has (Hoaxen's tentacles) works
            # only with its boss's armor; otherwise the game fails every frame
            "abilityArmor": {p: max(set(a), key=a.count) for p, a in (
                (p, [f["load"]["Armor"] for c in chars for f in c["forms"] if f.get("ability") == p and f["load"].get("Armor")])
                for p in body_bound(entries, www)) if a},
            "abilitySkeleton": ability_skeleton(entries),   # put on with the Super
            "enchantments": {t: sorted(n for n, x in ench.items() if x == t) for t in SLOTS},
            "extraPerks": {"fighter": extra},     # the own fighter's extra perk (extra_perks)
            "rules": fight_rules(entries, lang, tr_lang),
            "perks": perk_info(entries, lang, pnames),
            # the menu in Turkish: the game's Turkish texts (name, description) where it has them
            "perksTr": {n: [v[0], v[2], v[3]] for n, v in perk_info(entries, tr_lang, pnames, tr=True).items()},
            "superPerksTr": {n: tr_lang.get(n) or lang.get(n, n) for n in super_perks(entries)}}


# perks that can do nothing in a match here (checked by hand)
NOT_FOR_MATCHES = {
    "PERK_SPHERE_COOLDOWN": "the raid's sphere button, which matches do not have",
    "PERK_SPHERES_BALANCER": "scales the raid's sphere missiles (all factors 1)",
    "PERK_FIGHT_WITH_HOAXEN": "a cooldown for Hoaxen's own move (ShopMagicHungerClaws)",
    "PERK_TITAN_DEATH": "Titan's death animation",
    "PERK_PREDICTION": "its effects (mgc_effect_prediction_*) are in no version of the game",
    "PERK_TIME_SURGE": "its effect (mgc_surge_time_effec) is in no version of the game",
    "PERK_SHROUD_FALL": "Shroud's scripted fight ending (below 30 %: fake fall, Kali appears)",
    "PEKR_FREEZE_FALL": "a scripted fight ending (below 30 %: no more damage, then every hit kills)",
}

# Texts for perks the game has no text for (and a few it only names), from their data: (English,
# Turkish, numbers). A number is ["e", formula of Set values] (the menu redoes it with a fight's values) or a
# function of the Set values (fixed when built). Formulas: Frames / 60 = s; RegenerationRate r -> r * 60 /
# 10000 % of health a second; Lifesteal / 100 = %; DamageFactor d -> x 2^(d / 10000); a hit's Damage is a share
# of health (DamageConverter 1, measured), so Chance="?Hit[].Damage * k" is k x that share.
MULT = lambda d: round(2 ** (float(d) / 10000) * 100, 1)        # DamageFactor -> % of normal damage
PERK_TEXTS = {
    "PERK_BEGINNER": ("When the enemy has {0}% health or less, your blocks do not work.",
                      "Rakibin canı %{0} ya da altındayken blokların işe yaramaz.", [["e", "_Health * 100"]]),
    "PERK_BOSS_HERMIT_MAGIC": ("Every {0} s your magic charges by {1}% (until it is full).",
                               "Her {0} sn'de büyün %{1} dolar (dolana kadar).", [["e", "_Step / 60"], ["e", "_Charge * 100"]]),
    "PERK_BOSS_BUTCHER_BLEEDING": (
        "A weapon or ranged hit can make the enemy bleed {0}% health a second for {1} s. The chance is 5 times the "
        "hit's damage (a hit for 10% health: 50%).",
        "Silah ya da fırlatma vuruşu rakibi {1} sn boyunca saniyede %{0} kanatabilir. İhtimal, vuruşun hasarının 5 "
        "katı (%10 canlık vuruş: %50).", [["e", "0 - _RegenerationRate * 60 / 10000"], ["e", "_Frames / 60"]]),
    "PERK_WEAPON_BLOCK_BREAKER": ("Your weapon hits have a {0}% chance to go through the enemy's block.",
                                  "Silah vuruşlarının rakibin bloğunu delme ihtimali %{0}.", [["e", "_Chance * 100"]]),
    "PERK_GREATER_GOOD_SHARPENING": ("An unblocked weapon hit has a 15% chance to become a critical hit.",
                                     "Bloklanmayan silah vuruşunun kritik vuruşa dönme ihtimali %15.", []),
    "PERK_BOSS_WASP_DAMAGE_RETURN": (
        "When you are hit: a {0}% chance that the enemy loses {1}% of the damage he dealt.",
        "Vuruş aldığında: rakibin verdiği hasarın %{1} kadarını kendisinin kaybetme ihtimali %{0}.",
        [["e", "_Chance * 100"], ["e", "0 - _DamageReturn / 100"]]),
    "PERK_BOSS_WASP_REGENERATION": (
        "An unblocked hit you take can start a regeneration of {0}% health a second for {1} s. The chance is 3 "
        "times the hit's damage (a hit for 10% health: 30%).",
        "Bloklanmayan bir vuruş aldığında {1} sn boyunca saniyede %{0} can yenilenmesi başlayabilir. İhtimal, "
        "vuruşun hasarının 3 katı (%10 canlık vuruş: %30).",
        [["e", "_RegenerationRate * 60 / 10000"], ["e", "_Frames / 60"]]),
    "PERK_BOSS_WIDOW_LIFESTEAL": ("Each hit you land: a {0}% chance to get back {1}% of its damage as health.",
                                  "Her isabetinde: hasarın %{1} kadarını can olarak geri alma ihtimali %{0}.",
                                  [["e", "_Chance * 100"], ["e", "_Lifesteal / 100"]]),
    "PERK_BOSS_WIDOW_SHIELDING": (
        "When a hit comes at you (at most every {0} s): a {1}% chance that for {2} s the enemy deals only {3}% "
        "of his damage.",
        "Sana vuruş gelirken (en fazla {0} sn'de bir): {2} sn boyunca rakibin hasarının yalnızca %{3} kadarını "
        "verme ihtimali %{1}.",
        [["e", "_NegateFrames / 60"], ["e", "_Chance * 100"], ["e", "_Frames / 60"], lambda st: MULT(st["DamageFactor"])]),
    "PERK_BOSS_SHOGUN_FRENZY": (
        "When you hit (at most every {0} s): a {1}% chance of a {2} s frenzy, in which your weapon and unarmed hits "
        "deal {3}% damage and have a {4}% chance to go through a block.",
        "Vurduğunda (en fazla {0} sn'de bir): %{1} ihtimalle {2} sn'lik öfke; bu sürede silah ve çıplak el "
        "vuruşların %{3} hasar verir ve bloğu delme ihtimalleri %{4}.",
        [["e", "_NegateFarmes / 60"], ["e", "_Chance * 100"], ["e", "_Frames / 60"],
         lambda st: MULT(st["DamageFactor"]), ["e", "_BlockBreakChance * 100"]]),
    "GREATER_RANGED_DAMAGE": ("Your ranged hits deal 2^30 (about a billion) times their damage: one ranged hit wins the round.",
                              "Fırlatma vuruşların 2^30 (yaklaşık bir milyar) kat hasar verir: tek isabet raundu kazandırır.", []),
    "GREATER_MAGIC_DAMAGE": ("Your magic hits deal 2^30 (about a billion) times their damage: one magic hit wins the round.",
                             "Büyü vuruşların 2^30 (yaklaşık bir milyar) kat hasar verir: tek isabet raundu kazandırır.", []),
    "PERK_TITANS_SHIELD": ("A shield takes the next hit (it deals 0.1%); it comes back {0} s later.",
                           "Bir kalkan sıradaki vuruşu karşılar (vuruş %0.1 hasar verir); {0} sn sonra yeniden gelir.",
                           [["e", "_Cooldown / 60"]]),
    "PERK_ELIXIR_ROCK": ("For {0} s heavy, critical and shock hits do not knock you down.",
                         "{0} sn boyunca ağır, kritik ve şok vuruşlar seni yere düşürmez.", [["e", "_Frames / 60"]]),
    "PERK_CONFUSION": ("When your magic missile hits the enemy, your weapon and unarmed hits go through his block for {0} s.",
                       "Büyü mermin rakibe isabet edince {0} sn boyunca silah ve çıplak el vuruşların bloğunu deler.",
                       [["e", "_Frames / 60"]]),
    "PERK_ELIXIR_RESISTANCE": (
        "For {0} s: no shock or disarm from hits, and +{1} enchantment resistance at each hit (at level 52 "
        "enchantments are at their cap, so this hardly lowers their chance).",
        "{0} sn boyunca: vuruşlar şok etmez, silahını düşürtmez; her vuruşta +{1} tılsım direnci (52. seviyede "
        "tılsımlar tavandadır, bu yüzden ihtimallerini pek düşürmez).", [["e", "_Frames / 60"], ["e", "_Base"]]),
    "PERK_RAID_WEAPON_DAMAGE_ATTRIBUTE": ("Weapon damage attribute +{0}.", "Silah hasarı özelliği +{0}.", [["e", "_Value"]]),
    "PERK_ENCHANTMENT_RESISTANCE": (
        "After a critical hit you take: a chance (the hit's damage, a hit for 10% health: 10%) of +{0} enchantment "
        "resistance for {1} s (at level 52 this hardly lowers the enemy's enchantments).",
        "Kritik vuruş aldıktan sonra: bir ihtimalle (vuruşun hasarı kadar; %10 canlık vuruş: %10) {1} sn boyunca +{0} "
        "tılsım direnci (52. seviyede rakibin tılsımlarını pek etkilemez).", [["e", "_Base"], ["e", "_Frames / 60"]]),
    "PERK_ANTI_SHOCK": ("Hits do not shock or disarm you.", "Vuruşlar seni şok etmez, silahını düşürtmez.", []),
    "PERK_RULE_EARTHQUAKE": ("An earthquake every {0} s: whoever is on the ground loses {1}% health.",
                             "Her {0} sn'de bir deprem: o an yerde olan %{1} can kaybeder.",
                             [["e", "_Frames_quake / 60"], ["e", "_Health * 100"]]),
    "PERK_SHROUD_FALL": ("The scripted end of Shroud's fight (below 30% health); it does nothing in a match.",
                         "Shroud dövüşünün senaryolu sonu (%30 canın altında); maçta işe yaramaz.", []),
    "PERK_TITAN_DEATH": ("Titan's death animation.", "Titan'ın ölüm animasyonu.", []),
}


def extra_perks(entries, ench):
    """The own fighter's one extra perk (boss traits, elixirs ...): perk -> its Set values (the game's own use
    of it on a fighter). Perks the dojo cannot give. Left out: rule perks (hidden icon, an area, or only in a
    fight's <Rules>: fight_rules offers the game's rules), what other parts of the menu give (and the Default
    template's own perks), perks other perks build on (Template), abilities (built on PERK_BOSS_ABILITY: they
    are moves), perks an item gives, developer perks (debug lines), perks that only show an icon, test perks,
    perks a newer version replaced (_OLD), and NOT_FOR_MATCHES."""
    px = ET.fromstring(get_xml(entries, "res/perks.xml").lstrip("﻿"))
    perks = {p.get("Name"): p for p in px if p.tag == "Perk" and p.get("Name")}
    base = {p.get("Template") for p in perks.values()}
    cp, li = get_xml(entries, "res/character_progress.xml"), get_xml(entries, "res/list.xml")
    default = ET.fromstring(get_xml(entries, "res/stages.xml").lstrip("\ufeff")).find(".//Template[@Name='Default']")
    given = {p.get("Name") for p in default.iter("Perk")} | set(ability_perks(entries)) | set(super_perks(entries)) | set(ench) | \
        set(re.findall(r'<Perk Name="([^"]+)"', li)) | set(re.findall(r'\b(PERK_\w+)', cp))

    def chain(n):
        seen = []
        while n in perks and n not in seen:
            seen.append(n)
            n = perks[n].get("Template")
        return seen

    def acts(n):
        return {a.tag for t in perks[n].iter("Actions") for a in t}

    stages = ET.fromstring(get_xml(entries, "res/stages.xml").lstrip("﻿"))
    in_rules, on_fighters = set(), {}
    for f in stages.iter("Fight"):
        rules = f.find("Rules")
        in_rules |= set() if rules is None else {p.get("Name") for p in rules.iter("Perk")}
        for w in f.iter("Warrior"):
            for p in w.iter("Perk"):
                st = p.find("Set")
                on_fighters.setdefault(p.get("Name"), {} if st is None else dict(st.attrib))
    for t in stages.iter("Template"):
        for p in t.iter("Perk"):
            st = p.find("Set")
            on_fighters.setdefault(p.get("Name"), {} if st is None else dict(st.attrib))

    fighter, seen = {}, set()
    for n, p in perks.items():
        body = re.sub(r'^<Perk Name="[^"]*"', "", ET.tostring(p, encoding="unicode"))   # a copy under another name
        if body in seen:
            continue
        seen.add(body)
        if (n in given or n in NOT_FOR_MATCHES or n in base or "PERK_BOSS_ABILITY" in chain(n)[1:] or p.get("ItemSet")
                or "TEST" in n or n.endswith("_OLD") and n[:-4] in perks
                or "ShowDebugLine" in acts(n) or not acts(n) - {"ModIcon"}):
            continue
        area = re.search(r"<(AreaEnter|AreaExit|InTheArea)\b", ET.tostring(p, encoding="unicode"))
        if not area and p.get("Hidden") != "1" and (n in on_fighters or n not in in_rules):
            fighter[n] = on_fighters.get(n, {})
    return fighter

# The game's named rule bundles (stages.xml <ComplexRule><Description Alias=...>) offered for a match. Each
# applies to both fighters. Left out: rules that only make sense against one side (ENEMY_*, *_TIMEOUT_SURVIVE,
# DODGE_ENEMYS_MAGIC, THROW_AREA_DODGE_ENEMYS_RANGED, RANDOM_EQUIPMENT), a location change
# (SHRINKED_LOCATION_*), STYLE_* (the game has no text for every style), DARKNESS (FIGHT_IN_THE_DARK with
# another timing), and combinations of rules that are offered one by one.
FIGHT_RULES = ("FIGHT_IN_THE_DARK", "LIGHT_IN_THE_DARK", "NO_HEALTHBAR", "INVERTED_JOYSTICK", "NO_BLOCKS",
               "NO_DOUBLE", "NO_JUMPS", "NO_KICKS", "KICKS_ONLY", "NO_WEAPON", "NO_RANGED", "NO_MAGIC",
               "ENDURANCE", "HEALTH_DROPS", "HEALTH_REGENERATION", "HEALTH_DROPS_AND_LIFESTEAL", "HOT_GROUND",
               "RING_OUT", "NO_FALLS", "THROWS_ONLY", "FRAGILITY", "RANDOM_AREA_HEALTH_DEGENERATION",
               "HEALTH_DROPS_RANDOM_AREA_HEALTH_REGENERATION", "EARTHQUAKE", "VOLCANO", "THORNY_WALLS",
               "TRANSIENT_BLEEDING", "HARD_GROUND", "SCORE_CONTEST", "SCORE_CONTEST_CRITICAL_ONLY", "SCORE",
               "SCORE_HEAD_HIT", "COMBO")
RULE_DROP = {"Description", "ChangeFight", "SetTactic", "RatingEvaluation"}   # the fight's own settings
RULE_ATTRIBUTES = {"ENDURANCE", "COMBO"}   # rules whose Attributes are the rule (weak hits; hits do nothing),
                                           # not a balance for the challenge
RULE_NAME_TR = {"RING_OUT": "Bölge dışı yenilgi"}   # the game's Turkish says "time ran out and you lost"
RULE_FIX = {
    # the game's only use (Damage 0.1) takes 0.00001 % per fall (measured: nothing); without it the perk's
    # own value takes 10 % (measured)
    "HARD_GROUND": lambda r: [p.remove(p.find("Set")) for p in r.findall("Perk") if p.find("Set") is not None],
    # the game's fights with this rule also turn off wall jumps (NoAnimation Wall)
    "THORNY_WALLS": lambda r: ET.SubElement(r, "NoAnimation", Name="Wall"),
}


def fight_rules(entries, lang, tr_lang):
    """alias -> {"xml": the rule for a match's <Rules>, "area": bool, "win": bool, "name", "nameTr", "text",
    "textTr"}. The most used variant of each bundle in the game (the later one on a tie), made symmetric:
    Attributes go (a challenge's balance) unless they are the rule (RULE_ATTRIBUTES); an element for one side
    (ApplyTo) is given to both, with the player's values."""
    stages = ET.fromstring(get_xml(entries, "res/stages.xml").lstrip("﻿"))
    variants, seq = {}, 0
    for cr in stages.iter("ComplexRule"):
        d = cr.find("Description")
        alias = d.get("Alias") if d is not None else ""
        base, _, arg = alias.partition("{")
        if base not in FIGHT_RULES:
            continue
        r = ET.Element("ComplexRule")
        ET.SubElement(r, "Description", Alias=alias)
        sided = {}                                   # an element for one side -> both sides get the player's
        for e in cr:
            if e.tag in RULE_DROP or e.tag == "Attributes" and base not in RULE_ATTRIBUTES:
                continue
            if e.get("ApplyTo") is None:             # the game's rule for both (no ApplyTo means All)
                r.append(copy.deepcopy(e))
                continue
            k = (e.tag, e.get("Name") if e.tag == "Perk" else
                 tuple(sorted((a, v) for a, v in e.attrib.items() if a not in ("ApplyTo",) + (("DamageFactor",) if e.tag == "Attributes" else ()))))
            if e.get("ApplyTo") != "Bot" or k not in sided:
                sided[k] = e
        for e in sided.values():                     # ApplyTo="All" does not work for every rule (measured:
            for side in ("Player", "Bot"):           # Invulnerability, LoseFall), so one copy each, as the game does
                p = copy.deepcopy(e)
                p.set("ApplyTo", side)
                r.append(p)
        if base in RULE_FIX:
            RULE_FIX[base](r)
        for e in r.iter():                           # the game's indentation
            e.tail = None
            e.text = e.text if e.text and e.text.strip() else None
        xml = ET.tostring(r, encoding="unicode")
        v = variants.setdefault(base, {}).setdefault(xml, [r, arg.rstrip("}"), 0, 0])
        v[2] += 1
        seq += 1
        v[3] = seq                                   # when it was last seen
    out = {}
    for base in FIGHT_RULES:
        if base not in variants:
            continue
        xml, (r, arg, _, _) = max(variants[base].items(), key=lambda kv: (kv[1][2], kv[1][3]))
        fill = lambda t: (t or "").replace("{0}", arg)
        out[base] = {"xml": re.sub(r"<ComplexRule><Description Alias=\"[^\"]*\" />", "", xml)[:-len("</ComplexRule>")],
                     "alias": base + ("{%s}" % arg if arg else ""),
                     "area": r.find("RandomArea") is not None,
                     "win": any(r.find(t) is not None for t in ("Points", "WinCombo")),
                     "name": fill(lang.get(base, base)), "nameTr": fill(RULE_NAME_TR.get(base) or tr_lang.get(base) or lang.get(base, base)),
                     "text": rule_text(r, "en"), "textTr": rule_text(r, "tr")}
    return out


def rule_text(r, ln):
    """a rule's effect in words, its numbers from its values (game formulas: RegenerationRate 10000 = 1 %
    of health per frame, a Regeneration Rate is a share of health per frame, DamageFactor d -> x 2^(d/10000),
    frames / 60 = seconds; measured in matches: see the notes)"""
    s = lambda x: f"{x:g}"
    sec = lambda f: s(round(float(f) / 60, 1))
    pct = lambda x: s(round(x, 1))
    t = []
    tr = ln == "tr"
    for e in r:
        if e.get("ApplyTo") == "Bot":                # the same for both: said once
            continue
        g = e.get
        if e.tag == "Darkness":
            t.append(f"Ekran {sec(g('DarkLasting'))} sn kararır, {sec(g('LightLasting'))} sn aydınlanır; bu tekrarlanır." if tr else
                     f"The screen goes dark for {sec(g('DarkLasting'))} s, then light for {sec(g('LightLasting'))} s, over and over.")
        elif e.tag == "LightInTheDarkness":
            t.append("Ekran karanlık; yalnızca dövüşçülerin çevresi görünür." if tr else "The screen is dark; only a small light around the fighters.")
        elif e.tag == "NoHealthBar" and r.find("Points") is None:
            t.append("Sağlık çubukları gizli." if tr else "Health bars are hidden.")
        elif e.tag == "InvertJoystick":
            t.append("Sağ ve sol yön tuşları yer değiştirir." if tr else "Left and right are swapped.")
        elif e.tag == "RemoveInterval" and g("Type") == "Block" and r.find("Points") is None:
            t.append("Kimse blok yapamaz." if tr else "No one can block.")
        elif e.tag == "NoAnimation" and g("Name") == "Jump":
            t.append("Zıplamak yok." if tr else "No jumping.")
        elif e.tag == "NoAnimation" and g("Name") == "3key":
            t.append("Çift vuruş (iki tuş birden) yok." if tr else "No double strikes.")
        elif e.tag == "NoAnimation" and g("Name") == "Wall":
            t.append("Duvardan sekme yok." if tr else "No wall jumps.")
        elif e.tag == "NoButton":
            k = {"Punch": ("Yumruk", "Punch"), "Kick": ("Tekme", "Kick")}[g("Name")]
            t.append(f"{k[0]} tuşu kapalı." if tr else f"The {k[1].lower()} button is off.")
        elif e.tag == "EquipItem":
            k = {"Fists": ("silah yok", "no weapon"), "NoRanged": ("menzilli silah yok", "no ranged weapon"),
                 "NoMagic": ("büyü yok", "no magic")}.get(g("Name"))
            k and t.append(("Herkes için " + k[0] + ".") if tr else ("Both: " + k[1] + "."))
        elif e.tag == "Attributes" and g("DamageFactor"):
            x = 2 ** (float(g("DamageFactor")) / 10000) * 100
            t.append((f"Vuruş hasarı normalin %{pct(x)} kadarı." if x >= 0.1 else "Vuruşlar hasar vermez.") if tr else
                     (f"Hits deal {pct(x)}% of normal damage." if x >= 0.1 else "Hits deal no damage."))
        elif e.tag == "Regeneration":
            x, w = float(g("Rate")) * 60 * 100, sec(g("FramesAfterHit") or 0)
            if x < 0:
                t.append(f"Saniyede %{pct(-x)} can gider (vuruş alınca {w} sn durur)." if tr else
                         f"Health drops {pct(-x)}% per second (pauses {w} s after a hit taken).")
            else:
                t.append(f"Saniyede %{pct(x)} can dolar (vuruş alınca {w} sn durur)." if tr else
                         f"Health refills {pct(x)}% per second (pauses {w} s after a hit taken).")
        elif e.tag == "Lifesteal":
            x = float(g("DamagePart")) * 100
            t.append(f"Vuran, verdiği hasarın %{pct(x)} kadarını can olarak geri alır." if tr else f"Each hit heals the attacker by {pct(x)}% of its damage.")
        elif e.tag == "HotGround":
            t.append(f"Yerde {sec(g('Frames'))} sn kalan kaybeder." if tr else f"Staying on the ground for {sec(g('Frames'))} s loses.")
        elif e.tag == "Ringout":
            t.append("İşaretli bölgenin dışına çıkan kaybeder." if tr else "Leaving the marked zone loses.")
        elif e.tag == "LoseFall":
            t.append("Yere düşen kaybeder." if tr else "Being knocked down loses.")
        elif e.tag == "DamageFactor" and g("Animation") == "Throw":
            t.append("Yalnızca fırlatmalar hasar verir; bir fırlatma kazandırır." if tr else "Only throws deal damage; one throw wins.")
        elif e.tag == "Points":
            if g("Type") == "Contest":
                t.append(("Kimse can kaybetmez; süre bitince daha çok " + ("kritik " if g("Critical") else "") + "vuruş yapan kazanır. Blok yok.") if tr else
                         ("No one loses health; when time runs out, more " + ("critical " if g("Critical") else "") + "hits wins. No blocks."))
            else:
                head = g("Defense") == "HeadDefense"
                t.append((f"Kimse can kaybetmez; ilk {g('Max')} " + ("kafa " if head else "") + "vuruşu yapan kazanır. Blok yok.") if tr else
                         (f"No one loses health; the first to {g('Max')} " + ("head " if head else "") + "hits wins. No blocks."))
        elif e.tag == "WinCombo":
            t.append(f"{g('Value')} vuruşluk kombo yapan hemen kazanır." if tr else f"A {g('Value')}-hit combo wins at once.")
        elif e.tag == "Perk":
            t.append(rule_perk_text(e, r, tr))
    return " ".join(x for x in t if x)


def rule_perk_text(e, r, tr):
    s = lambda x: f"{x:g}"
    st = e.find("Set")
    v = lambda k, d: float(st.get(k)) if st is not None and st.get(k) else d
    n = e.get("Name")
    if n == "PERK_RULE_EARTHQUAKE":       # measured: 5 % to whoever is on the ground (Health 0.05), every 9 s
        return (f"Her {s(v('Frames_quake', 540) / 60)} sn'de bir deprem olur; o an yerde olan %{s(v('Health', 0.05) * 100)} can kaybeder (zıplayan kurtulur)." if tr else
                f"An earthquake every {s(v('Frames_quake', 540) / 60)} s: whoever is on the ground then loses {s(v('Health', 0.05) * 100)}% health (a jump avoids it).")
    if n == "PERK_RULE_ENERGY_PILLAR":    # measured: a pillar under a fighter, about 7 % a hit
        lo, hi = v("FramesBase", 180) / 60, (v("FramesBase", 180) + v("FramesRange", 360)) / 60
        return (f"{s(lo)}-{s(hi)} sn'de bir dövüşçünün altından enerji sütunu çıkar (isabet: yaklaşık %7 can; blokla durmaz)." if tr else
                f"Every {s(lo)}-{s(hi)} s an energy pillar rises under a fighter (a hit: about 7% health; blocking does not stop it).")
    if n == "PERK_RULE_THORNS":           # Base -10000 for 10 frames: 10 % (measured)
        return ("Duvarlar dikenli: duvara değen %10 can kaybeder." if tr else "The walls are thorny: touching one costs 10% health.")
    if n == "HARD_GROUND":
        return ("Her yere düşüşte %10 can gider." if tr else "Each fall to the ground costs 10% health.")
    if n == "PERK_TRANSIENT_BLEEDING":    # RegenerationRate r -> r * 60 / 10000 % per second (measured)
        x = -v("AttributeBleeding", -600) * 60 / 10000
        return (f"Vuruş alan yanmaya başlar (saniyede %{s(x)}); vurduğunda yanma rakibe geçer." if tr else
                f"A fighter who is hit starts burning ({s(x)}% health per second); hitting back passes it on.")
    if n == "PERK_AREA_HEALTH_DEGENERATION":
        x = -v("AttributeBleeding", -500) * 60 / 10000
        return (f"İşaretli alanın içinde saniyede %{s(x)} can gider." if tr else f"Inside the marked area: {s(x)}% health lost per second.")
    if n == "PERK_AREA_HEALTH_REGENERATION":
        x = v("AttributeRegeneration", 500) * 60 / 10000
        return (f"İşaretli alanın içinde saniyede %{s(x)} can dolar." if tr else f"Inside the marked area: {s(x)}% health back per second.")
    if n == "PERK_AREA_FRAGILITY":
        x = 2 ** (v("AttributeDamageFactor", 10000) / 10000)
        return (f"İşaretli alanın içindeki dövüşçü {s(round(x, 2))} kat hasar alır." if tr else f"A fighter inside the marked area takes {s(round(x, 2))}x damage.")
    if n == "PERK_ANTI_SHOCK":
        return ""
    return ""


PERCENT_TRANSFORMS = (                   # how a level-up text shows a Set value
    lambda x: x * 100,                   # Chance 0.2 -> 20 (%)
    lambda x: x,
    lambda x: x / 60,                    # Frames 180 -> 3 (s)
    lambda x: (2 ** (x / 10000) - 1) * 100,   # DamageFactor 4330 -> 35 (% more: 2^(0.0001 DF), internal_settings)
    lambda x: -x / 100,                  # Lifesteal -4000 -> 40 (% returned)
    lambda x: 150 * 2 ** (x / 10000))    # a critical hit's damage (Avenger: DamageFactor 931 -> 160 %); kept only
                                         # where it fits every level, as each transform is


def progress_values(entries):
    """Perk description numbers. character_progress.xml has each level-up's text with its numbers
    (Description="PERKDESCRIPTION_COBRA{40}{3}{10}") and Set values (Chance="0.4" Health="0.1").
    A number that matches a Set value (through PERCENT_TRANSFORMS) at every level -> ("fits", [(key,
    transform)]); a number that never changes -> ("", number). Returns perk -> {index: ...}"""
    cp = get_xml(entries, "res/character_progress.xml")
    out = {}
    for p in re.finditer(r'<Perk Name="([^"]+)">(.*?)</Perk>', cp, re.S):
        lv, seen = [], set()                         # one entry per level: PERK_MIRROR has level 9 twice, the
        for u in re.finditer(r'<UpgradeLevel\b([^>]*)Description="([^"]*)"[^>]*>\s*<Set\b([^>]*)/>', p.group(2)):
            v = re.search(r'Value="([^"]*)"', u.group(1))   # second's text (52.5) not its Chance (0.5)
            if v and v.group(1) in seen:
                continue
            v and seen.add(v.group(1))
            lv.append((re.findall(r"\{([^}]*)\}", u.group(2)), dict(re.findall(r'(\w+)="([^"]*)"', u.group(3)))))
        if not lv:
            continue
        m = {}
        for i in range(min(len(a) for a, _ in lv)):
            try:
                v = [float(a[i]) for a, _ in lv]
            except ValueError:
                continue
            fits = []                                # every Set value + transform that fits (the texts
            for k in lv[0][1]:                       # show numbers rounded: within 1)
                for f in PERCENT_TRANSFORMS:
                    try:
                        if max(abs(f(float(s[k])) - x) for (_, s), x in zip(lv, v)) < 1:
                            fits.append((k, f))
                    except (KeyError, ValueError):
                        pass
            if fits:
                m[i] = ("fits", fits)
            elif len(set(v)) == 1 and len(lv) > 1:
                m[i] = ("", v[0])
        out[p.group(1)] = m
    return out


def fill_one(spec, st):
    """one {i} of a perk text from Set values st (as the menu's fillOne)"""
    if spec[0] == "c":
        return f"{spec[1]:g}"
    if spec[0] == "t":
        return f"{round(PERCENT_TRANSFORMS[spec[2]](float(st[spec[1]])), 1):g}" if spec[1] in st else "…"
    x = re.sub(r"_(\w+)", lambda m: st.get(m.group(1), "?"), spec[1])
    return f"{round(eval(x, {'__builtins__': {}}), 2):g}" if re.fullmatch(r"[\d.\s+\-*/()]+", x) else "…"


def fill_text(text, specs, st):
    return re.sub(r"\{(\d+)\}", lambda m: fill_one(specs[int(m.group(1))], st) if int(m.group(1)) in specs else "…",
                  text).replace("%%", "%")


def near_word(text, i, st):
    """Among Set values that all fit a level-up text's {i}, picks the one the game's sentence names next
    to it: "chance"/"şans" Chance (%), "seconds"/"saniye" Frames (/60), "damage"/"attack"/"hasar"/"saldırı"
    DamageFactor (% more); the word right after first, then the words before. Only values the data fits."""
    m = re.search(r"\{%d\}" % i, text)
    after = re.sub(r"^[%\s]+", "", text[m.end():].lower())
    before = text[:m.start()].lower()[-40:]
    rules = ((("chance", "şans"), "Chance", 0), (("second", "saniye"), "Frames", 2),
             (("damage", "attack", "hasar", "saldırı"), "DamageFactor", 3))
    for words, k, t in rules:
        if k in st and after.startswith(words):
            return ["t", k, t]
    for words, k, t in rules[::-1]:
        if k in st and any(w in before.rsplit(".", 1)[-1] for w in words) and k == "DamageFactor":
            return ["t", k, t]
    return None


def perk_info(entries, lang, names, tr=False):
    abil = ability_perks(entries)
    """the forms' traits for the menu: perk -> [name, slot, description]. An item special (an enchantment,
    e.g. PERK_ITEM_SPECIAL_TIME_BOMB_WEAPON -> ENCHANTMENT_TIME_BOMB) belongs to the slot its name ends
    with; other perks belong to the character (slot "")"""
    px = get_xml(entries, "res/perks.xml")
    perks = {m.group(1): dict(re.findall(r'(\w+)="([^"]*)"', m.group(0)))
             for m in re.finditer(r'<Perk Name="([^"]+)"[^>]*>', px)}
    sets = {m.group(1): dict(re.findall(r'(\w+)="([^"]*)"', m.group(2)))
            for m in re.finditer(r'<Perk Name="([^"]+)"[^>]*>\s*<Set\b([^>]*)/>', px)}

    known = progress_values(entries)
    progress_text = get_xml(entries, "res/character_progress.xml")
    asp = aspect(entries, enchant_aspect(entries, MAX_LEVEL))   # an enchantment's chance factor in a match

    def chain_names(n):
        seen = []
        while n in perks and n not in seen:
            seen.append(n)
            n = perks[n].get("Template")
        return seen

    def chain(n, get):                               # the first value up the Template chain
        seen = set()
        while n in perks and n not in seen:
            seen.add(n)
            v = get(n)
            if v:
                return v
            n = perks[n].get("Template")

    def describe(n):
        """the game's description text. Description="KEY{_Frames / 60}" fills its numbers from the
        perk's Set values with the game's own formula. PERKDESCRIPTION_<NAME> only for perks of the dojo's
        tree, whose level-up texts give its numbers: other perks named like an old text (PERK_CONFUSION
        and PERKDESCRIPTION_CONFUSION describe different things) get none. "" when the game has no text"""
        st, t, seen = {}, n, set()                    # Set values up the Template chain (the perk's own win)
        while t in perks and t not in seen:
            seen.add(t)
            st = {**sets.get(t, {}), **st}
            t = perks[t].get("Template")
        d = chain(n, lambda k: perks[k].get("Description"))
        if d:
            key, args = d.split("{", 1)[0], re.findall(r"\{([^}]*)\}", d)
        else:                                        # the dojo tree's own text (character_progress names it)
            key, args = "PERKDESCRIPTION_" + n.removeprefix("PERK_"), None
            key = key if key in progress_text else ""
        text = lang.get(key, "")
        if not text and n in abil:                   # a boss ability: the game has no text; what perks.xml says (a
            body, t, seen = "", n, set()                 # value counts only where a trigger uses it: _Frames, _InitialFrames)
            while t in perks and t not in seen:
                seen.add(t)
                body += (re.search(r'<Perk Name="%s"[^>]*>(.*?)</Perk>' % re.escape(t), px, re.S) or re.match("", "")).group(0)
                t = perks[t].get("Template")
            fr = st.get("Frames") if '"_Frames"' in body else None
            first = st.get("InitialFrames") if '"_InitialFrames"' in body else None
            if tr:
                return ("Super tuşu." + (f" Kullanıldıktan {round(float(fr) / 60, 1):g} sn sonra yeniden hazır" if fr else "")
                        + (f", ilk kez rauntun {round(float(first) / 60, 1):g}. saniyesinde." if first else "." if fr else ""))
            return ("Super button." + (f" Ready again {round(float(fr) / 60, 1):g} s after use" if fr else "")
                    + (f", first {round(float(first) / 60, 1):g} s into a round." if first else "." if fr else ""))
        if not text and n in PERK_TEXTS:              # no game text: written from the perk's data
            en, trt, nums = PERK_TEXTS[n]
            specs = {i: x if isinstance(x, list) else ["c", x(st)] for i, x in enumerate(nums)}
            text = trt if tr else en
            return fill_text(text, specs, st), {"t": text, "f": {str(i): v for i, v in specs.items()}, "base": st}
        if not text:
            return ""
        # each {i}: how its number comes from the Set values (the menu recomputes it with a fight's values):
        # ["e", formula] from the perk's own Description, ["t", key, transform] from a level-up text,
        # ["c", number] the same at every level; otherwise a word next to it (a chance, seconds, damage)
        specs = {}
        for i in map(int, re.findall(r"\{(\d+)\}", text)):
            x = args[i] if args and i < len(args) else None
            if x is not None and "_" in x and fill_one(["e", x], st) != "…":
                specs[i] = ["e", x]                  # the perk's own formula of its Set values
                continue
            k, f = known.get(n, {}).get(i, (None, None))
            fits = [["t", h, PERCENT_TRANSFORMS.index(g)] for h, g in f if h in st] if k == "fits" else []
            if len({tuple(c) for c in fits}) > 1 and len({fill_one(c, st) for c in fits}) > 1:
                # several Set values fit every level's text (PERK_DISORIENTATION: chance % = damage % at each level);
                # of those, the one the game's sentence names next to it
                w = near_word(text, i, st)
                fits = [w] if w in fits else []
            if fits:
                specs[i] = fits[0]                   # a level-up text's number = this Set value (also where the
            elif x is not None and fill_one(["e", x], st) != "…":   # perk's literal says the same: a fight's own
                specs[i] = ["e", x]                  # value then shows)
            elif k == "":
                specs[i] = ["c", f]                  # the same number at every level
        # the chance an enchantment's power gives, at level 52 against an enemy without enchantment resistance
        # (the game's text only says "depends on the power"): Random Chance="_Chance * ?Aspect[...]", or
        # "?Hit[].Damage / ...DamageConverter * _ChanceFactor * ?Aspect[...]" (a hit's damage: a share of health).
        # Not for chances of ?Hit[].BaseDamage, whose scale is not known here
        body = "".join(re.search(r'<Perk Name="%s"[^>]*>.*?</Perk>' % re.escape(t), px, re.S).group(0)
                       for t in chain_names(n))
        i = max(specs, default=-1) + 1
        if "Chance" in st and re.search(r'Random Chance="_Chance \* \?Aspect\[', body):
            specs[i] = ["c", round(min(100, float(st["Chance"]) * 100 * asp), 1)]
            text += (" 52. seviyede ihtimal: %%{%d}." if tr else " Chance at level 52: {%d}%%%%.") % i
        elif "ChanceFactor" in st and re.search(r'Random Chance="\?Hit\[\]\.Damage / \?PlayerParameter\[\w+\]\.DamageConverter \* _ChanceFactor \* \?Aspect\[', body):
            specs[i] = ["e", f"_ChanceFactor * {round(asp, 4):g}"]
            text += (" 52. seviyede ihtimal: vuruşun aldırdığı her %%1 can için %%{%d}." if tr else
                     " Chance at level 52: {%d}%%%% for each 1%%%% of health the hit takes.") % i
        fill = {"t": text, "f": {str(i): v for i, v in specs.items()}, "base": st} if specs else None
        return fill_text(text, specs, st), fill
    out = {}
    for n in names:
        alias, t, seen = None, n, set()
        while t in perks and t not in seen and not alias:
            seen.add(t)
            alias, t = perks[t].get("Alias"), perks[t].get("Template")
        slot = n.rsplit("_", 1)[-1]
        slot = slot.capitalize() if alias and slot in ("WEAPON", "RANGED", "MAGIC", "HELM", "ARMOR") else ""
        name = lang.get(alias or n) or functools.reduce(lambda x, f: x.replace(*f), ID_FIXES, n).removeprefix("PERK_").replace("_", " ").title()
        r = describe(n)                              # a Super's own text: no numbers to redo
        text, fill = r if isinstance(r, tuple) else (r, None)
        out[n] = [name, slot, text, fill]
    return out


def abilities_on_super(entries):
    """puts every ability on the Super button. A few boss abilities start on another key for a player
    (PERK_HERMITSTORM, PERK_RAT_WAVE: Up; PERK_SPORE_OF_DECAY: "Ability", which nothing here presses);
    their key becomes Super. The AI is not affected: it starts moves by their conditions"""
    other = {p for p, k in ability_perks(entries).items() if k != "Super"}
    for e in entries:
        if e[0] != "res/moves.xml":
            continue
        text = e[1].decode("utf-8")

        def move(m):
            lk = re.search(r'<Locks>(.*?)</Locks>', m.group(0), re.S)
            if not lk or not other & set(re.findall(r'<Perk Name="([^"]+)"', lk.group(1))):
                return m.group(0)
            return re.sub(r'(<Key Type=")(?:Up|Ability)(")', r'\1Super\2', m.group(0))
        e[1] = re.sub(r'<Move Name="[^"]+".*?</Move>', move, text, flags=re.S).encode("utf-8")


def item_title(n):
    """a name for an item the game has no text for: MAGIC_ICE_PINS -> Ice Pins"""
    return re.sub(r"^(MAGIC|RANGED|WEAPON|HELM|ARMOR)_", "", n).replace("_", " ").title()


def item_names(entries, www, profile):
    """the profile's items without a name text (boss magic like MAGIC_ICE_PINS was never in the shop: the
    game shows %%ERROR%%) get item_title() in every language file -> {path: new file}"""
    have = set(re.findall(r'<Word Title="([^"]+)"', open(glob.glob(os.path.join(www, "res", "lang", "en.*.xml"))[0],
                                                        encoding="utf-8").read()))
    names = {n for n in re.findall(r'Name="([^"]+)"', profile.decode("utf-8", "replace") if isinstance(profile, bytes)
                                   else profile)} & {i["Name"] for i in parse_items(entries)}
    names -= set(start_equipment(entries))          # Fists, NoMagic, ...: "nothing worn", never shown by name
    words = "".join(f'<Word Title="{n}">{item_title(n)}</Word>' for n in sorted(names - have))
    out = {}
    for f in glob.glob(os.path.join(www, "res", "lang", "*.xml")):
        text = open(f, encoding="utf-8").read()
        assert text.count("</Words>") == 1, f
        out[os.path.relpath(f, www)] = text.replace("</Words>", words + "</Words>").encode("utf-8")
    return out


def level_items(entries):
    """Fixes items stronger than their own next upgrade (event items: Snow Blind, ARMOR_ANNIVERSARY_10TH,
    the HW16 set). Their first upgrade would make them weaker. Such an item gets the values of its own
    Level (the level the game shows): its table's step at Level*100, or, for a table starting above that
    level, the first step scaled (level 2 of a table starting at 3: 2/3). Its UpgradeLevel becomes
    Level*100, so the first upgrade is the table's next step. Works for any item, by its table."""
    for e in entries:
        if e[0] != "res/list.xml":
            continue
        text = e[1].decode("utf-8")
        tables = {m.group(1): [dict(re.findall(r'(\w+)="([^"]*)"', u)) for u in re.findall(r'<Upgrade\b[^>]*/>', m.group(2))]
                  for m in re.finditer(r'<Upgrades Name="([^"]+)"[^>]*>(.*?)</Upgrades>', text, re.S)}

        def fix(m):
            a = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
            t = re.search(r'<Upgrades Template="([^"]+)"', m.group(2))
            steps = tables.get(t.group(1)) if t else None
            stat = DAMAGE_ATTR.get(a.get("Type"))
            if not steps or not stat or stat not in a:
                return m.group(0)
            lv = int(a.get("UpgradeLevel") or 0)
            later = [u for u in steps if int(u["UpgradeLevel"]) > lv and stat in u]
            if not later or float(a[stat]) <= float(min(later, key=lambda u: int(u["UpgradeLevel"]))[stat]):
                return m.group(0)
            own = int(a["Level"]) * 100 if a.get("Level") else lv // 100 * 100   # no Level (Sphere1): its UpgradeLevel's
            at = [u for u in steps if int(u["UpgradeLevel"]) == own]
            first = min(steps, key=lambda u: int(u["UpgradeLevel"]))
            k = 1 if at else int(a.get("Level") or 1) / int(first.get("Level") or 1)
            ref = at[0] if at else first
            head = re.sub(r'\bUpgradeLevel="[^"]*"', 'UpgradeLevel="%d"' % own, m.group(1))
            meta = {"Level", "Price", "BonusPrice", "DeliveryTime", "BonusDeliveryPrice", "UpgradeLevel", "Milestone"}
            for n in set(ref) - meta:                    # every value the table steps set (BodyDefense, UnarmedDamage, ...)
                if n in a:
                    head = re.sub(r'\b%s="[^"]*"' % n, '%s="%d"' % (n, round(float(ref[n]) * k)), head)
            return "<Item" + head + ">" + m.group(2) + "</Item>"
        text = re.sub(r'<Item\b([^>]*\bType="[^"]*"[^>]*)>(.*?)</Item>', fix, text, flags=re.S)
        e[1] = text.encode("utf-8")


# ------------------------------------------------------------------------------- the normal game
# The menu's "Normal game": the game as it is (its own sf2.js, xml2.dat and default profile, without the
# match patches), with its own save in this browser (SF2User/SF2Packs become SF2NormalUser/...; the
# matches' profile never touches it). Energy is unlimited (like owning the "Unlimited_Energy" item);
# "?cheat" also tops up money and gems at every load. normal.html adds a small panel: export / import
# the save (the game's own .sf2 file, ka.yqb / ka.yeb) and back to the menu
NORMAL_PATCHES = [
    ("own save", 'static H2a(a){return new Gk(a)}', 'static H2a(a){return new Gk(a.replace("SF2","SF2Normal"))}'),
    ("own settings", 'fg.j7="SF2Flags"', 'fg.j7="SF2NormalFlags"'),   # (a match profile change resets SF2Flags)
    ("unlimited energy", 'this.qU(this.ya.Uj("Unlimited_Energy")!=null)', 'this.qU(!0)'),
    ("cheat: money and gems", 'this.Pr(yb(a.attributes.get("Money"),0));this.zl(yb(a.attributes.get("Bonus"),0),0);',
     'this.Pr(window.__SF2_CHEAT?99999999:yb(a.attributes.get("Money"),0));'
     'this.zl(window.__SF2_CHEAT?999999:yb(a.attributes.get("Bonus"),0),0);'),
    ("save tools for the panel", 'Ka.SF2=Ka.SF2||{};', 'Ka.SF2=Ka.SF2||{};Object.defineProperty(window,"__sf2Save",{get:function(){return ka}});'),
]
NORMAL_PANEL = r"""<script>
window.__SF2_CHEAT=/cheat/.test(location.search);
window.__sf2Back=function(){location.href='index.html?menu';return true};   // Android's back: the match menu
// the APK: a WebView downloads nothing - the save's file (a blob link the game clicks) goes to the app's "save as"
(function(){var blobs={},mk=URL.createObjectURL,ck=HTMLAnchorElement.prototype.click;
  URL.createObjectURL=function(b){var u=mk.apply(URL,arguments);blobs[u]=b;return u};
  HTMLAnchorElement.prototype.click=function(){var b=blobs[this.href],n=this.download;
    if(window.SF2Native&&SF2Native.saveFile&&b&&n){b.text().then(function(t){SF2Native.saveFile(n,t)});return}
    return ck.apply(this,arguments)}})();
addEventListener('DOMContentLoaded',function(){
  var tr=/^tr/i.test(navigator.language||''),T=function(e,t){return tr?t:e};
  var b=document.createElement('button'),p=document.createElement('div');
  b.textContent='☰';b.style.cssText='position:fixed;left:6px;bottom:6px;z-index:99999;font:16px sans-serif;color:#f3c66b;'+
    'background:rgba(0,0,0,.55);border:1px solid #f3c66b;border-radius:10px;padding:2px 9px;opacity:.7';
  p.style.cssText='position:fixed;left:6px;bottom:40px;z-index:99999;display:none;background:rgba(0,0,0,.85);border:1px solid #f3c66b;'+
    'border-radius:10px;padding:8px;font:14px sans-serif';
  [[T('Export save (.sf2)','Kaydı dışa aktar (.sf2)'),function(){__sf2Save.yqb()}],
   [T('Import save (.sf2)','Kayıt içe aktar (.sf2)'),function(){__sf2Save.yeb()}],
   [T('Match menu','Maç menüsü'),function(){__sf2Back()}]].forEach(function(x){
    var k=document.createElement('button');k.textContent=x[0];k.onclick=function(){p.style.display='none';x[1]()};
    k.style.cssText='display:block;width:100%;margin:3px 0;color:#f3c66b;background:#2a1d10;border:1px solid #f3c66b;border-radius:8px;padding:6px 10px';
    p.appendChild(k)});
  b.onclick=function(){p.style.display=p.style.display=='none'?'':'none'};
  document.body.appendChild(p);document.body.appendChild(b)});
</script>"""


def normal_game(www, xml2_path, js_path):
    """the files of the normal game -> {path: bytes}"""
    js = open(js_path, encoding="utf-8").read()
    prof = sorted(glob.glob(os.path.join(www, "res", "users_default.*.xml")))
    # under other names (the game finds a file by NAME.HASH.EXT, 8 hex digits: its own copies stay the matches')
    other = lambda f: re.sub(r"\.[0-9a-f]{8}\.", "." + hashlib.md5(open(f, "rb").read() + b"normal").hexdigest()[:8] + ".",
                             os.path.basename(f))
    names = [(os.path.basename(f), other(f)) for f in [xml2_path] + prof]
    name, pat, repl = SAVE_PATCH
    if len(re.findall(pat, js, flags=re.S)) != 1:
        sys.exit(f"normal game: '{name}' not found once in {os.path.basename(js_path)} (game update?)")
    js = re.sub(pat, lambda m: repl, js, flags=re.S)
    for name, find, repl in NORMAL_PATCHES + [("file " + a, a, b) for a, b in names]:
        if js.count(find) != 1:
            sys.exit(f"normal game: '{name}' found {js.count(find)} times in {os.path.basename(js_path)} (game update?)")
        js = js.replace(find, repl)
    html = open(os.path.join(www, "index.html"), encoding="utf-8").read()
    if html.count(os.path.basename(js_path)) != 1 or "</head>" not in html:
        sys.exit("normal game: index.html is not what the builder knows")
    html = html.replace(os.path.basename(js_path), "sf2.normal.js").replace("</head>", NORMAL_PANEL + "</head>", 1)
    out = {"sf2.normal.js": js.encode("utf-8"), "normal.html": html.encode("utf-8")}
    for f in [xml2_path] + prof:
        out[os.path.join("res", other(f))] = open(f, "rb").read()
    return out


def build_game(www, out, entries, xml2_path):
    """the patched game, the same for every match: sf2.js (patches and __SF2DATA), xml2.dat (tutorial
    quests emptied, the all-items profile, every ability on Super), index.html, the portal config"""
    PROGRESS.stage(0, 3, "game data: items, moves, the all-items profile")
    abilities_on_super(entries)                      # first: the data below reads moves.xml
    level_items(entries)                             # and list.xml
    profile = all_items_profile(entries, www)
    for e in entries:
        if e[0] == "res/users_default.xml":
            e[1] = profile
        elif e[0] == "res/quests.xml":
            e[1] = keep_quests(e[1])
        elif e[0].startswith("res/quest_extensions/"):
            root = re.match(rb"\s*<(\w+)", e[1]).group(1)
            e[1] = b"<" + root + b"></" + root + b">"
    PROGRESS.stage(3, 6, "xml2.dat and the game's code patches (sf2.js)")
    xml2 = zstandard.ZstdCompressor(level=10).compress(pack(entries))
    js_path = sorted(glob.glob(os.path.join(www, "sf2.*.js")))[0]
    js = patch_js(open(js_path, encoding="utf-8").read()).encode("utf-8")
    PROGRESS.stage(6, 10, "match data (sf2data.js)")
    rd = runtime_data(entries, www, profile)
    PROGRESS.stage(10, 85, "menu data: characters, forms, item pictures")
    md = menu_data(entries, www, out)
    data = ("window.__SF2DATA=" + json.dumps(rd) + ";\n" + "window.__SF2MENU=" + json.dumps(md) + ";\n").encode("utf-8")
    PROGRESS.stage(85, 90, "lists, pages, the normal game")
    patched = {
        os.path.relpath(xml2_path, www): xml2,
        os.path.relpath(js_path, www): js,
        "famobi.json": private_famobi(www),
        "sf2data.js": data,
        "sf2list.json": json.dumps(list_data(entries, www)).encode("utf-8"),
        "match.js": b"window.__SF2_CONFIG=null;\n",   # no match: the menu (server.py serves its own match.js)
        os.path.join("sf2menu", "menu.js"): MENU_JS.encode("utf-8"),
        "index.html": home_screen_app(www),
        "sf2.webmanifest": json.dumps({"name": "Shadow Fight 2", "short_name": "SF2", "start_url": ".",
                                       "display": "fullscreen", "orientation": "landscape",
                                       "background_color": "#000000", "icons": [
                                           {"src": os.path.basename(f), "sizes": "180x180", "type": "image/png"}
                                           for f in glob.glob(os.path.join(www, "apple-touch-icon.*.png"))]}).encode(),
    }
    for f in glob.glob(os.path.join(www, "res", "users_default.*.xml")):
        patched[os.path.relpath(f, www)] = profile   # the same default profile also ships as a loose file
    patched.update(item_names(entries, www, profile))
    patched.update(normal_game(www, xml2_path, js_path))
    PROGRESS.stage(90, 100, "writing the game folder")
    build(www, out, patched)


# =============================================================================== APK
# The app (android/sf2_shell.apk, built once from android/project: WebView, bridge, network server) has
# no game in it. --apk adds the output folder as assets/www, aligns and signs it. Signing needs Java
# (java, keytool); android/apksigner.jar is Google's signer from the Android SDK.
ANDROID = os.path.join(os.path.dirname(os.path.abspath(__file__)), "android")
APK_SKIP = (".dds", ".ogg")       # Android's WebView has no S3TC (uses .ktx) and plays .m4a
APK_STORE = (".webp", ".avif", ".m4a", ".mp4", ".dat", ".png", ".jpg")   # compressed already
KEY_ALIAS, KEY_PASS = "sf2", "sf2match"


def java_tool(name):
    p = shutil.which(name)
    if not p and os.environ.get("JAVA_HOME"):
        p = shutil.which(name, path=os.path.join(os.environ["JAVA_HOME"], "bin"))
    if not p:
        sys.exit(f"--apk: '{name}' not found - install Java (e.g. Temurin 17) and put it on PATH or set JAVA_HOME")
    return p


def zip_aligned(zf, name, data, store):
    """add a file; a stored one starts at a multiple of 4 bytes (what zipalign does: Android maps
    uncompressed assets and resources.arsc straight from the APK)"""
    zi = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
    zi.compress_type = zipfile.ZIP_STORED if store else zipfile.ZIP_DEFLATED
    if store:
        start = zf.fp.tell() + 30 + len(name.encode("utf-8"))
        pad = (-(start + 6)) % 4
        zi.extra = struct.pack("<HHH", 0xD935, 2 + pad, 4) + b"\0" * pad   # zipalign's own padding field
    zf.writestr(zi, data, compresslevel=6)


def build_apk(out, apk):
    shell = os.path.join(ANDROID, "sf2_shell.apk")
    signer = os.path.join(ANDROID, "apksigner.jar")
    for f in (shell, signer):
        if not os.path.exists(f):
            sys.exit(f"--apk: {f} not found - the android folder must sit next to builder.py, as it comes")
    java, keytool = java_tool("java"), java_tool("keytool")
    ks = os.path.join(ANDROID, "sf2.keystore")
    if not os.path.exists(ks):     # once: every later APK must carry the same key (see the notes)
        subprocess.run([keytool, "-genkeypair", "-keystore", ks, "-alias", KEY_ALIAS, "-keyalg", "RSA",
                        "-keysize", "2048", "-validity", "36500", "-storepass", KEY_PASS, "-keypass", KEY_PASS,
                        "-dname", "CN=SF2 Match"], check=True, stdout=subprocess.DEVNULL)
        PROGRESS.say(PROGRESS.lo, f"new signing key {ks} - keep it: an APK with another key does not install over this one")
    tmp = apk + ".unsigned"
    n = 0
    game = [os.path.join(root, f) for root, _, files in os.walk(out) for f in sorted(files) if not f.endswith(APK_SKIP)]
    PROGRESS.stage(0, 90, f"APK: packing {len(game)} game files")
    with zipfile.ZipFile(shell) as src, zipfile.ZipFile(tmp, "w") as zf:
        for zi in src.infolist():
            if zi.filename.startswith("META-INF/") or zi.filename.startswith("assets/www/"):
                continue                       # the shell's own signature; an older game
            zip_aligned(zf, zi.filename, src.read(zi), zi.compress_type == zipfile.ZIP_STORED)
        for p in game:
            name = "assets/www/" + os.path.relpath(p, out).replace(os.sep, "/")
            with open(p, "rb") as fh:
                zip_aligned(zf, name, fh.read(), p.endswith(APK_STORE))
            n += 1
            if n % 1000 == 0:
                PROGRESS.part(n, len(game), f"APK: {n} of {len(game)} files packed")
    PROGRESS.stage(90, 100, "APK: signing")
    r = subprocess.run([java, "-jar", signer, "sign", "--ks", ks, "--ks-key-alias", KEY_ALIAS,
                        "--ks-pass", "pass:" + KEY_PASS, "--key-pass", "pass:" + KEY_PASS,
                        "--out", apk, tmp], capture_output=True, text=True)
    # Java 24+ warns on every run that the signer's crypto library loads native code: not ours to fix
    noise = ("restricted method", "System::loadLibrary", "--enable-native-access", "Restricted methods")
    for line in (r.stdout + r.stderr).splitlines():
        if r.returncode or line.strip() and not any(n in line for n in noise):
            print(line)
    if r.returncode:
        sys.exit("--apk: signing failed")
    os.remove(tmp)
    for f in (apk + ".idsig",):
        os.path.exists(f) and os.remove(f)
    PROGRESS.say(PROGRESS.hi, f"APK ready: {apk} ({os.path.getsize(apk) // 2**20} MB, {n} game files)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="the game's www folder (the original); with --apk also a built out folder")
    ap.add_argument("-o", "--out", help="output folder (default: <www>_out next to www)")
    ap.add_argument("--apk", nargs="?", const="", metavar="FILE", help="also the Android app (default: <out>.apk)")
    ap.add_argument("--dump-xml", metavar="FIGHT", help="print a game fight's <Fight> (ZONE/BATTLE/FIGHT) and stop")
    ap.add_argument("--android", metavar="FOLDER", help="android.py's folder whose content is added (default: the "
                    "newest android_* next to www; none = without)")
    a = ap.parse_args(argv)
    www = orig = os.path.abspath(a.folder)
    if os.path.exists(os.path.join(www, "sf2list.json")):     # a folder this builder made
        if a.apk is None:
            sys.exit(f"{a.folder} is a built game, not the original www (--apk makes the app from it)")
        PROGRESS.start()
        return build_apk(www, os.path.abspath(a.apk or www.rstrip("\\/") + ".apk"))
    out = os.path.abspath(a.out or orig.rstrip("\\/") + "_out")
    if out == orig or out.startswith(orig + os.sep):
        sys.exit("the output folder must be outside the www folder")
    found = sorted(glob.glob(os.path.join(os.path.dirname(www), "android_*", "apk", "version.txt")),
                   key=lambda f: [int(x) for x in re.findall(r"\d+", open(f).read())])
    work = None if a.android == "none" else os.path.abspath(a.android) if a.android else \
        os.path.dirname(os.path.dirname(found[-1])) if found else None
    # the copy sits next to www (hard links need the same drive) and is named after both, so two builds
    # from one www to different outputs do not collide
    temp = orig.rstrip("\\/") + "." + os.path.basename(out.rstrip("\\/")) + ".android_tmp" if work else None
    # each part's share of the percent shown
    apk = a.apk is not None
    windows = {(True, True): [(0, 70), (70, 80), (80, 100)], (True, False): [(0, 90), (90, 100)],
               (False, True): [(0, 40), (40, 100)], (False, False): [(0, 100)]}[(bool(work), apk)]
    PROGRESS.start()
    try:
        if work:                                             # www stays as it is: a copy (hard links) gets the content
            import android
            PROGRESS.window(*windows.pop(0))
            PROGRESS.stage(0, 0, f"Android content from {work}")
            android.import_into(www, work, temp)
            www = temp
        PROGRESS.window(*windows.pop(0))
        xml2_path, entries = load_entries(www)
        if a.dump_xml:
            z, bt, f = (a.dump_xml.split("/") + ["", "", ""])[:3]
            el = ET.fromstring(get_xml(entries, "res/stages.xml")).find(
                f'.//Zone[@Name="{z}"]/Battle[@Name="{bt}"]/Fight[@Name="{f}"]')
            if el is None:
                sys.exit(f"--dump-xml: no fight {a.dump_xml} (server.py <out> --list battles)")
            return print(ET.tostring(el, encoding="unicode"))
        build_game(www, out, entries, xml2_path)
    finally:
        temp and os.path.exists(temp) and shutil.rmtree(temp)
    PROGRESS.say(PROGRESS.hi, f"game ready: {out}")
    if apk:
        PROGRESS.window(*windows.pop(0))
        build_apk(out, os.path.abspath(a.apk or out.rstrip("\\/") + ".apk"))


if __name__ == "__main__":
    sys.modules.setdefault("builder", sys.modules["__main__"])   # android.py's "import builder": this run
    main()
