import Link from "next/link";
import { prisma } from "@/lib/prisma";

export default async function PostsPage() {
  const posts = await prisma.post.findMany({
    where: { published: true },
    include: {
      author: { select: { id: true, name: true } },
      tags: true,
      _count: { select: { likes: true, comments: true } },
    },
    orderBy: { createdAt: "desc" },
  });

  const allTags = await prisma.tag.findMany({
    orderBy: { name: "asc" },
  });

  return (
    <div>
      <div className="flex items-center justify-between mb-8">
        <h1 className="text-3xl font-bold text-gray-900">All Posts</h1>
      </div>

      {allTags.length > 0 && (
        <div className="flex flex-wrap gap-2 mb-6">
          {allTags.map((tag) => (
            <Link
              key={tag.id}
              href={`/tags/${tag.name}`}
              className="bg-gray-100 text-gray-700 px-3 py-1 rounded-full text-sm hover:bg-gray-200"
            >
              {tag.name}
            </Link>
          ))}
        </div>
      )}

      <div className="space-y-6">
        {posts.map((post) => (
          <article
            key={post.id}
            className="bg-white rounded-lg shadow-sm border border-gray-200 p-6"
          >
            <Link href={`/posts/${post.id}`}>
              <h2 className="text-xl font-semibold text-gray-900 hover:text-blue-600 mb-2">
                {post.title}
              </h2>
            </Link>
            <p className="text-gray-600 mb-4">
              {post.content.slice(0, 200)}
              {post.content.length > 200 ? "..." : ""}
            </p>
            <div className="flex items-center gap-4 text-sm text-gray-500">
              <Link
                href={`/profile/${post.author.id}`}
                className="hover:text-blue-600"
              >
                {post.author.name}
              </Link>
              <span>{new Date(post.createdAt).toLocaleDateString()}</span>
              <span>{post._count.likes} likes</span>
              <span>{post._count.comments} comments</span>
            </div>
            {post.tags.length > 0 && (
              <div className="flex gap-2 mt-3">
                {post.tags.map((tag) => (
                  <Link
                    key={tag.id}
                    href={`/tags/${tag.name}`}
                    className="bg-gray-100 text-gray-600 px-2 py-0.5 rounded text-sm hover:bg-gray-200"
                  >
                    {tag.name}
                  </Link>
                ))}
              </div>
            )}
          </article>
        ))}
        {posts.length === 0 && (
          <p className="text-gray-500 text-center py-12">
            No posts yet. Be the first to write one!
          </p>
        )}
      </div>
    </div>
  );
}
