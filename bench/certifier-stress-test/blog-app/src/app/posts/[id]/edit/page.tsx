"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";

export default function EditPostPage({ params }: { params: { id: string } }) {
  const router = useRouter();
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [post, setPost] = useState({
    title: "",
    content: "",
    published: false,
    tags: "",
  });

  useEffect(() => {
    fetch(`/api/posts/${params.id}`)
      .then((res) => res.json())
      .then((data) => {
        setPost({
          title: data.title,
          content: data.content,
          published: data.published,
          tags: data.tags.map((t: { name: string }) => t.name).join(", "),
        });
        setLoading(false);
      });
  }, [params.id]);

  async function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();

    const tags = post.tags
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);

    const res = await fetch(`/api/posts/${params.id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: post.title,
        content: post.content,
        published: post.published,
        tags,
      }),
    });

    if (!res.ok) {
      const data = await res.json();
      setError(data.error || "Failed to update post");
      return;
    }

    router.push(`/posts/${params.id}`);
  }

  async function handleDelete() {
    if (!confirm("Are you sure you want to delete this post?")) return;

    const res = await fetch(`/api/posts/${params.id}`, { method: "DELETE" });

    if (res.ok) {
      router.push("/posts");
    }
  }

  if (loading) {
    return <div className="text-gray-500">Loading...</div>;
  }

  return (
    <div className="max-w-2xl mx-auto">
      <h1 className="text-2xl font-bold text-gray-900 mb-6">Edit Post</h1>
      {error && (
        <div className="bg-red-50 text-red-600 p-3 rounded-md mb-4">
          {error}
        </div>
      )}
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label
            htmlFor="title"
            className="block text-sm font-medium text-gray-700 mb-1"
          >
            Title
          </label>
          <input
            id="title"
            value={post.title}
            onChange={(e) => setPost({ ...post, title: e.target.value })}
            required
            className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>
        <div>
          <label
            htmlFor="content"
            className="block text-sm font-medium text-gray-700 mb-1"
          >
            Content
          </label>
          <textarea
            id="content"
            value={post.content}
            onChange={(e) => setPost({ ...post, content: e.target.value })}
            rows={12}
            required
            className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>
        <div>
          <label
            htmlFor="tags"
            className="block text-sm font-medium text-gray-700 mb-1"
          >
            Tags (comma-separated)
          </label>
          <input
            id="tags"
            value={post.tags}
            onChange={(e) => setPost({ ...post, tags: e.target.value })}
            className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>
        <div className="flex items-center gap-2">
          <input
            id="published"
            type="checkbox"
            checked={post.published}
            onChange={(e) => setPost({ ...post, published: e.target.checked })}
            className="rounded"
          />
          <label htmlFor="published" className="text-sm text-gray-700">
            Published
          </label>
        </div>
        <div className="flex gap-4">
          <button
            type="submit"
            className="bg-blue-600 text-white px-4 py-2 rounded-md hover:bg-blue-700"
          >
            Update Post
          </button>
          <button
            type="button"
            onClick={handleDelete}
            className="bg-red-600 text-white px-4 py-2 rounded-md hover:bg-red-700"
          >
            Delete Post
          </button>
        </div>
      </form>
    </div>
  );
}
