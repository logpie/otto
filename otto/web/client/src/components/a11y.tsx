import {useEffect} from "react";

/**
 * Toggle `inert` + `aria-hidden` on every element matching `selector`.
 */
export function InertEffect({active, selector, ariaHidden = false}: {active: boolean; selector: string; ariaHidden?: boolean}) {
  useEffect(() => {
    if (typeof document === "undefined") return;
    const nodes = Array.from(document.querySelectorAll<HTMLElement>(selector));
    if (!nodes.length) return;
    const previous = nodes.map((node) => ({
      inert: node.hasAttribute("inert"),
      ariaHidden: node.getAttribute("aria-hidden"),
    }));
    if (active) {
      for (const node of nodes) {
        node.setAttribute("inert", "");
        if (ariaHidden) node.setAttribute("aria-hidden", "true");
      }
    } else {
      for (const node of nodes) {
        node.removeAttribute("inert");
        node.removeAttribute("aria-hidden");
      }
    }
    return () => {
      nodes.forEach((node, idx) => {
        const prev = previous[idx];
        if (prev?.inert) node.setAttribute("inert", "");
        else node.removeAttribute("inert");
        if (prev?.ariaHidden === null || prev?.ariaHidden === undefined) {
          node.removeAttribute("aria-hidden");
        } else {
          node.setAttribute("aria-hidden", prev.ariaHidden);
        }
      });
    };
  }, [active, selector, ariaHidden]);
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
