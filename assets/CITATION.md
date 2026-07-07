# Image Asset Attribution

The 27 JPEG images in this directory (`image_0000.jpg` – `image_0026.jpg`) were originally
collected by the benchmark's image-search agent at first-run time. The agent queried the
following public sources and downloaded a representative sample of openly licensed images:

---

## Original Sources

### NASA Image and Video Library
- **Query:** Mars Pathfinder terrain imagery
- **API:** `https://images-api.nasa.gov/search`
- **Asset resolver:** `https://images-api.nasa.gov/asset/{nasa_id}`
- **License:** NASA imagery is generally in the public domain unless otherwise noted.
  See [NASA Media Usage Guidelines](https://www.nasa.gov/nasa-brand-center/images-and-media/).

### Wikimedia Commons
- **Queries:** Mars/Pathfinder images; cell microscopy images
- **API:** `https://commons.wikimedia.org/w/api.php`
- **Browse:** `https://commons.wikimedia.org/wiki/Category:Cells`,
  `https://commons.wikimedia.org/wiki/Category:Microscopy`
- **License:** Individual files carry their own license (CC BY, CC BY-SA, or public domain).
  Check each file's Wikimedia page for the exact terms.

### Flickr (open-license filter)
- **Queries:** `cell microscopy`, `human cells microscope`
- **Search URL:** `https://www.flickr.com/search/?text=cell%20microscopy&license=2%2C3%2C4%2C5%2C6%2C9`
- **License codes used:** 2 (CC BY-NC), 3 (CC BY-NC-SA), 4 (CC BY), 5 (CC BY-SA),
  6 (CC BY-ND), 9 (CC0/Public Domain). Only images with these open licenses were fetched.

### Cell Image Library
- **URL:** `https://www.cellimagelibrary.org/`
- **License:** Images are freely available for research and educational use.
  See the site's individual image pages for attribution details.

### Protein Atlas
- **URL:** `https://www.proteinatlas.org/`
- **License:** Images are licensed under [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/)
  unless otherwise stated. Attribution: Human Protein Atlas, proteinatlas.org.

---

## Current Status

The external image-search agent (`agents/image-search-agent.py`) has been removed from this
repository. The 27 images above are now shipped statically as the benchmark workload and are
loaded from disk at runtime with no network calls required.

If you need to refresh or replace the image set, source images directly from the URLs above,
ensuring you respect the license terms of each source.

---

## Notes on Use

These images are used exclusively as an **input workload** for measuring OpenCV processing
throughput on AWS Graviton instances. No images are redistributed externally or used for
training, inference, or any purpose beyond local benchmarking.
