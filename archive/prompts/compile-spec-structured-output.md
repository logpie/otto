## Structured output channel

If your runtime provides a structured output field named `spec_json`, put the
complete Spec JSON object in that field as a serialized JSON string. Otherwise
write `{spec_path}` and use the `<spec_json>` fallback exactly as instructed
above.
