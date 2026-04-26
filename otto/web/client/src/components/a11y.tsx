import {useEffect} from "react";

/**
 * Toggle the `inert` attribute on every element matching `selector`.
 */
export function InertEffect({active, selector}: {active: boolean; selector: string}) {
  useEffect(() => {
    if (typeof document === "undefined") return;
    const nodes = Array.from(document.querySelectorAll<HTMLElement>(selector));
    if (!nodes.length) return;
    const previous = nodes.map((node) => node.hasAttribute("inert"));
    if (active) {
      for (const node of nodes) node.setAttribute("inert", "");
    } else {
      for (const node of nodes) node.removeAttribute("inert");
    }
    return () => {
      nodes.forEach((node, idx) => {
        if (previous[idx]) node.setAttribute("inert", "");
        else node.removeAttribute("inert");
      });
    };
  }, [active, selector]);
  return null;
}

/**
 * Polite singleton aria-live region.
 */
export function LiveRegion({message}: {message: string}) {
  return (
    <div
      id="mc-live-region"
      role="status"
      aria-live="polite"
      aria-atomic="true"
      className="sr-only"
      data-testid="mc-live-region"
    >
      {message}
    </div>
  );
}
