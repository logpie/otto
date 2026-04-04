"use client";

import { useSession } from "next-auth/react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import StarRating from "@/components/StarRating";
import Link from "next/link";

interface Recipe {
  id: string;
  title: string;
  description: string;
  ingredients: string[];
  instructions: string;
  prepTime: number;
  cookTime: number;
  servings: number;
  category: string;
  createdAt: Date;
  author: { id: string; name: string };
  ratings: { value: number; userId: string }[];
  favorites: { userId: string }[];
  avgRating: number;
  ratingCount: number;
}

const categoryColors: Record<string, string> = {
  breakfast: "bg-yellow-100 text-yellow-800",
  lunch: "bg-green-100 text-green-800",
  dinner: "bg-blue-100 text-blue-800",
  dessert: "bg-pink-100 text-pink-800",
};

export default function RecipeDetail({ recipe }: { recipe: Recipe }) {
  const { data: session } = useSession();
  const router = useRouter();
  const userId = (session?.user as { id?: string })?.id;

  const userRating = recipe.ratings.find((r) => r.userId === userId)?.value || 0;
  const isFavorited = recipe.favorites.some((f) => f.userId === userId);
  const isAuthor = userId === recipe.author.id;

  const [currentRating, setCurrentRating] = useState(userRating);
  const [favorited, setFavorited] = useState(isFavorited);

  const handleRate = async (value: number) => {
    if (!session) {
      router.push("/login");
      return;
    }
    setCurrentRating(value);
    await fetch(`/api/recipes/${recipe.id}/rate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    });
    router.refresh();
  };

  const handleFavorite = async () => {
    if (!session) {
      router.push("/login");
      return;
    }
    setFavorited(!favorited);
    await fetch(`/api/recipes/${recipe.id}/favorite`, { method: "POST" });
    router.refresh();
  };

  const handleDelete = async () => {
    if (!confirm("Are you sure you want to delete this recipe?")) return;
    await fetch(`/api/recipes/${recipe.id}`, { method: "DELETE" });
    router.push("/");
  };

  return (
    <div className="max-w-3xl mx-auto">
      <div className="bg-white rounded-lg shadow-sm border p-8">
        <div className="flex items-center justify-between mb-4">
          <span
            className={`text-sm font-medium px-3 py-1 rounded-full ${categoryColors[recipe.category] || "bg-gray-100 text-gray-800"}`}
          >
            {recipe.category}
          </span>
          <div className="flex items-center gap-3">
            {session && (
              <button
                onClick={handleFavorite}
                className={`text-2xl ${favorited ? "text-red-500" : "text-gray-300"} hover:text-red-500`}
              >
                &#9829;
              </button>
            )}
            {isAuthor && (
              <>
                <Link
                  href={`/recipes/${recipe.id}/edit`}
                  className="text-sm text-gray-600 hover:text-gray-900 px-3 py-1 border rounded"
                >
                  Edit
                </Link>
                <button
                  onClick={handleDelete}
                  className="text-sm text-red-600 hover:text-red-800 px-3 py-1 border border-red-200 rounded"
                >
                  Delete
                </button>
              </>
            )}
          </div>
        </div>

        <h1 className="text-3xl font-bold text-gray-900 mb-2">
          {recipe.title}
        </h1>
        <p className="text-gray-600 mb-4">{recipe.description}</p>
        <p className="text-sm text-gray-500 mb-6">
          By {recipe.author.name}
        </p>

        <div className="flex items-center gap-4 mb-6">
          <StarRating rating={recipe.avgRating} readonly size="sm" />
          <span className="text-sm text-gray-500">
            {recipe.ratingCount > 0
              ? `${recipe.avgRating.toFixed(1)} (${recipe.ratingCount} ratings)`
              : "No ratings yet"}
          </span>
        </div>

        <div className="grid grid-cols-3 gap-4 mb-8 p-4 bg-gray-50 rounded-lg">
          <div className="text-center">
            <div className="text-sm text-gray-500">Prep Time</div>
            <div className="font-semibold">{recipe.prepTime} min</div>
          </div>
          <div className="text-center">
            <div className="text-sm text-gray-500">Cook Time</div>
            <div className="font-semibold">{recipe.cookTime} min</div>
          </div>
          <div className="text-center">
            <div className="text-sm text-gray-500">Servings</div>
            <div className="font-semibold">{recipe.servings}</div>
          </div>
        </div>

        <div className="mb-8">
          <h2 className="text-xl font-semibold mb-3">Ingredients</h2>
          <ul className="list-disc list-inside space-y-1">
            {recipe.ingredients.map((ing: string, i: number) => (
              <li key={i} className="text-gray-700">
                {ing}
              </li>
            ))}
          </ul>
        </div>

        <div className="mb-8">
          <h2 className="text-xl font-semibold mb-3">Instructions</h2>
          <div className="text-gray-700 whitespace-pre-line">
            {recipe.instructions}
          </div>
        </div>

        {session && (
          <div className="border-t pt-6">
            <h3 className="text-lg font-semibold mb-2">Rate this recipe</h3>
            <StarRating rating={currentRating} onRate={handleRate} />
          </div>
        )}
      </div>
    </div>
  );
}
