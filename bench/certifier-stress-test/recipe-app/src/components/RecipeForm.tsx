"use client";

import { useState } from "react";

interface RecipeFormProps {
  onSubmit: (data: {
    title: string;
    description: string;
    ingredients: string[];
    instructions: string;
    prepTime: number;
    cookTime: number;
    servings: number;
    category: string;
  }) => void;
  saving: boolean;
  initial?: {
    title: string;
    description: string;
    ingredients: string[];
    instructions: string;
    prepTime: number;
    cookTime: number;
    servings: number;
    category: string;
  };
}

const categories = ["breakfast", "lunch", "dinner", "dessert"];

export default function RecipeForm({ onSubmit, saving, initial }: RecipeFormProps) {
  const [title, setTitle] = useState(initial?.title || "");
  const [description, setDescription] = useState(initial?.description || "");
  const [ingredients, setIngredients] = useState<string[]>(
    initial?.ingredients || [""]
  );
  const [instructions, setInstructions] = useState(initial?.instructions || "");
  const [prepTime, setPrepTime] = useState(initial?.prepTime || 0);
  const [cookTime, setCookTime] = useState(initial?.cookTime || 0);
  const [servings, setServings] = useState(initial?.servings || 4);
  const [category, setCategory] = useState(initial?.category || "dinner");

  const addIngredient = () => setIngredients([...ingredients, ""]);
  const removeIngredient = (index: number) =>
    setIngredients(ingredients.filter((_, i) => i !== index));
  const updateIngredient = (index: number, value: string) => {
    const updated = [...ingredients];
    updated[index] = value;
    setIngredients(updated);
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onSubmit({
      title,
      description,
      ingredients: ingredients.filter((i) => i.trim()),
      instructions,
      prepTime,
      cookTime,
      servings,
      category,
    });
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="bg-white p-8 rounded-lg shadow-sm border space-y-6"
    >
      <div>
        <label className="block text-sm font-medium text-gray-700 mb-1">
          Title
        </label>
        <input
          type="text"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          required
          className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-orange-500"
        />
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-700 mb-1">
          Description
        </label>
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          required
          rows={2}
          className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-orange-500"
        />
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-700 mb-1">
          Category
        </label>
        <select
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-orange-500"
        >
          {categories.map((cat) => (
            <option key={cat} value={cat}>
              {cat.charAt(0).toUpperCase() + cat.slice(1)}
            </option>
          ))}
        </select>
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-700 mb-2">
          Ingredients
        </label>
        {ingredients.map((ing, i) => (
          <div key={i} className="flex gap-2 mb-2">
            <input
              type="text"
              value={ing}
              onChange={(e) => updateIngredient(i, e.target.value)}
              placeholder={`Ingredient ${i + 1}`}
              className="flex-1 px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-orange-500"
            />
            {ingredients.length > 1 && (
              <button
                type="button"
                onClick={() => removeIngredient(i)}
                className="px-3 py-2 text-red-600 hover:bg-red-50 rounded-lg"
              >
                Remove
              </button>
            )}
          </div>
        ))}
        <button
          type="button"
          onClick={addIngredient}
          className="text-sm text-orange-600 hover:underline"
        >
          + Add ingredient
        </button>
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-700 mb-1">
          Instructions
        </label>
        <textarea
          value={instructions}
          onChange={(e) => setInstructions(e.target.value)}
          required
          rows={6}
          className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-orange-500"
        />
      </div>

      <div className="grid grid-cols-3 gap-4">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Prep Time (min)
          </label>
          <input
            type="number"
            value={prepTime}
            onChange={(e) => setPrepTime(Number(e.target.value))}
            min={0}
            className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-orange-500"
          />
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Cook Time (min)
          </label>
          <input
            type="number"
            value={cookTime}
            onChange={(e) => setCookTime(Number(e.target.value))}
            min={0}
            className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-orange-500"
          />
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Servings
          </label>
          <input
            type="number"
            value={servings}
            onChange={(e) => setServings(Number(e.target.value))}
            min={1}
            className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-orange-500"
          />
        </div>
      </div>

      <button
        type="submit"
        disabled={saving}
        className="w-full bg-orange-600 text-white py-2 rounded-lg hover:bg-orange-700 font-medium disabled:opacity-50"
      >
        {saving ? "Saving..." : "Save Recipe"}
      </button>
    </form>
  );
}
