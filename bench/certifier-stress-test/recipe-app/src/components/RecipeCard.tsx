import Link from "next/link";

interface RecipeCardProps {
  id: string;
  title: string;
  description: string;
  category: string;
  prepTime: number;
  cookTime: number;
  servings: number;
  authorName: string;
  avgRating: number;
  ratingCount: number;
}

const categoryColors: Record<string, string> = {
  breakfast: "bg-yellow-100 text-yellow-800",
  lunch: "bg-green-100 text-green-800",
  dinner: "bg-blue-100 text-blue-800",
  dessert: "bg-pink-100 text-pink-800",
};

export default function RecipeCard({
  id,
  title,
  description,
  category,
  prepTime,
  cookTime,
  servings,
  authorName,
  avgRating,
  ratingCount,
}: RecipeCardProps) {
  return (
    <Link href={`/recipes/${id}`}>
      <div className="bg-white rounded-lg shadow-sm border hover:shadow-md transition-shadow p-6 h-full flex flex-col">
        <div className="flex items-center justify-between mb-2">
          <span
            className={`text-xs font-medium px-2.5 py-0.5 rounded ${categoryColors[category] || "bg-gray-100 text-gray-800"}`}
          >
            {category}
          </span>
          <div className="flex items-center gap-1 text-sm text-gray-500">
            <span className="text-yellow-400">&#9733;</span>
            <span>
              {ratingCount > 0 ? avgRating.toFixed(1) : "No ratings"}
            </span>
            {ratingCount > 0 && <span>({ratingCount})</span>}
          </div>
        </div>
        <h3 className="text-lg font-semibold text-gray-900 mb-1">{title}</h3>
        <p className="text-sm text-gray-600 mb-4 flex-1 line-clamp-2">
          {description}
        </p>
        <div className="flex items-center justify-between text-xs text-gray-500">
          <span>
            {prepTime + cookTime} min &middot; {servings} servings
          </span>
          <span>by {authorName}</span>
        </div>
      </div>
    </Link>
  );
}
