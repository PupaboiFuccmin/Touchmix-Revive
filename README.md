# Touchmix-Revive

Reverse engineering the QSC TouchMix-16 Wi-Fi control protocol so the mixer can
be controlled by third-party software.

* [`docs/PROTOCOL.md`](docs/PROTOCOL.md): wire format, message types, parameter layout, capture log
* [`docs/params.csv`](docs/params.csv): all 5,694 parameter IDs with names, from the mixer's own config
* [`tools/tmcap.py`](tools/tmcap.py): decoder for PCAPdroid captures (Python 3, standard library only)

## Quick start

```sh
python3 tools/tmcap.py summary capture.pcap          # flows and message counts
python3 tools/tmcap.py params  capture.pcap          # every parameter change, with names
python3 tools/tmcap.py files   capture.pcap out/     # extract and inflate the .dscomp XML files
python3 tools/tmcap.py dict    out/<conn>-NH4-config.xml docs/params.csv
```

Captures are not committed (see `.gitignore`). They include your network
details and the mixer's full scene.
