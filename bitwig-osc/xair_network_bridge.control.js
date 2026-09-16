// X-Air Network Bridge — Bitwig controller script
// Same OSC surface as REAPER so start-reaper-sync / osc-launcher work.
// Load: Bitwig Settings → Controllers → Add → Hardware → Script
//       point at this file. Do not copy into ~/Bitwig Studio from this launcher
//       (user config is left untouched).
//
// JACK: xair_net_bridge:NN_*_out = Bitwig inputs (dry from X18)
//       xair_net_bridge:NN_*_in  = Bitwig outputs (duplex graph)
// Plugins: always on the OUTPUT chain / post-fader send to USB Return.

loadAPI(17);

host.defineController(
  "X-Air Network Bridge",
  "X-Air Network Bridge OSC",
  "1.0.0",
  "c0ffee00-18a1-4b17-9e00-000000000018",
  "X-Air"
);
host.defineMidiPorts(0, 0);

var TRACKS = 18;
var OSC_OUT_HOST = "127.0.0.1";
var OSC_OUT_PORT = 9001;
var OSC_IN_PORT = 8000;

function init() {
  println("X-Air Network Bridge OSC init  in=" + OSC_IN_PORT + "  out=" + OSC_OUT_HOST + ":" + OSC_OUT_PORT);

  var osc = host.getOscModule();
  var space = osc.createAddressSpace();
  space.setShouldConsumeEvents(false);

  var bank = host.createMainTrackBank(TRACKS, 1, 0);
  var i;
  for (i = 0; i < TRACKS; i++) {
    (function (idx) {
      var t = bank.getItemAt(idx);
      t.name().markInterested();
      t.volume().markInterested();
      t.pan().markInterested();
      t.mute().markInterested();
    })(i);
  }

  space.registerMethod("/track/{n}/volume", "*", function (source, path, types, args) {
    var n = parseInt(path.split("/")[2], 10);
    if (n >= 1 && n <= TRACKS && args.length) {
      bank.getItemAt(n - 1).volume().set(args[0], 1);
    }
  });
  space.registerMethod("/track/{n}/pan", "*", function (source, path, types, args) {
    var n = parseInt(path.split("/")[2], 10);
    if (n >= 1 && n <= TRACKS && args.length) {
      bank.getItemAt(n - 1).pan().set(args[0], 1);
    }
  });
  space.registerMethod("/track/{n}/mute", "*", function (source, path, types, args) {
    var n = parseInt(path.split("/")[2], 10);
    if (n >= 1 && n <= TRACKS && args.length) {
      bank.getItemAt(n - 1).mute().set(!!args[0]);
    }
  });
  space.registerMethod("/track/{n}/name", "*", function (source, path, types, args) {
    var n = parseInt(path.split("/")[2], 10);
    if (n >= 1 && n <= TRACKS && args.length) {
      bank.getItemAt(n - 1).name().set(String(args[0]));
    }
  });

  try {
    osc.createUdpServer(OSC_IN_PORT, space);
    println("X-Air Network Bridge OSC listening UDP " + OSC_IN_PORT);
  } catch (e) {
    println("X-Air Network Bridge: could not bind OSC " + OSC_IN_PORT + " (" + e + ")");
  }

  try {
    osc.connectToUdpServer(OSC_OUT_HOST, OSC_OUT_PORT, space);
    println("X-Air Network Bridge OSC TX " + OSC_OUT_HOST + ":" + OSC_OUT_PORT);
  } catch (e) {
    println("X-Air Network Bridge: OSC TX failed (" + e + ")");
  }

  println("USB Return: post-fader send from stems to a stereo FX track; plugins on OUTPUT only.");
}

function flush() {}
function exit() {
  println("X-Air Network Bridge OSC exit");
}
