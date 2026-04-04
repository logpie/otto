"use client";

import { useState } from "react";

interface StarRatingProps {
  rating: number;
  onRate?: (value: number) => void;
  readonly?: boolean;
  size?: "sm" | "md" | "lg";
}

const sizes = { sm: "text-lg", md: "text-2xl", lg: "text-3xl" };

export default function StarRating({
  rating,
  onRate,
  readonly = false,
  size = "md",
}: StarRatingProps) {
  const [hover, setHover] = useState(0);

  return (
    <div className="flex gap-0.5">
      {[1, 2, 3, 4, 5].map((star) => (
        <button
          key={star}
          type="button"
          disabled={readonly}
          onClick={() => onRate?.(star)}
          onMouseEnter={() => !readonly && setHover(star)}
          onMouseLeave={() => setHover(0)}
          className={`${sizes[size]} ${readonly ? "cursor-default" : "cursor-pointer"} ${
            star <= (hover || rating) ? "text-yellow-400" : "text-gray-300"
          }`}
        >
          &#9733;
        </button>
      ))}
    </div>
  );
}
