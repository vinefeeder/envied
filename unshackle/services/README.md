A collection of non-premium services for Unshackle.

## Usage:
Clone repository:

`git clone https://github.com/stabbedbybrick/services.git`

Add folder to `unshackle.yaml`:

```
directories:
    services: "path/to/services"
```
See help text for each service:

`unshackle dl SERVICE --help`

## Notes:
Some versions of the dependencies work better than others. These are the recommended versions as of 25/11/11:

- Shaka Packager: [v2.6.1](https://github.com/shaka-project/shaka-packager/releases/tag/v2.6.1)
- CCExtractor: [v0.93](https://github.com/CCExtractor/ccextractor/releases/tag/v0.93)
- MKVToolNix: [latest](https://mkvtoolnix.download/downloads.html)
- FFmpeg: [latest](https://ffmpeg.org/download.html)