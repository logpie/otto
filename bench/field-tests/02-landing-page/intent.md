# HTML/JS Landing Page

Build a polished single-page landing site for a fictional neighborhood bakery
called "Loaf & Light".

Required behavior:

- Use plain HTML, CSS, and JavaScript. No build step is required.
- The first viewport should show the bakery name, a concise offer, hours, and a
  primary order button.
- Include sections for today's menu, pre-order pickup slots, location, and a
  small customer note form.
- The note form should validate name and message client-side and then show a
  visible confirmation state without a backend.
- Include a responsive layout that works on phone and desktop widths.
- Include `start.sh` at the repo root. It must serve the page on `$PORT` and
  default to `8000` only when `$PORT` is not set.
- Include `tests/run_acceptance.py` that starts the static server or inspects
  the files and verifies key text, form JavaScript, responsive CSS, and that
  `start.sh` exists and is executable.

Keep the app small. This should be a real usable page, not a framework demo.
