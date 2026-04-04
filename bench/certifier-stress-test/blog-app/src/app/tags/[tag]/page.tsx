import Link from "next/link";
import { notFound } from "next/navigation";
import { prisma } from "@/lib/prisma";

export default async function TagPage({
  params,
}: {
  params: { tag: string };
}) {
  const tagName = decodeURIComponent(params.tag);

  const tag = await prisma.tag.findUnique({
    where: { name: tagName },
    include: {
      posts: {
        where: { published: true },
        include: {
          author: { select: { id: true, name: true } },
          tags: true,
          _count: { select: { likes: true, comments: true } },
        },
        orderBy: { createdAt: "desc" },
      },
    },
  });

  if (!tag) notFound();

  return (
    <div>
      <div className="mb-8">
        <Link href="/posts" className="text-blue-600 hover:underline text-sm">
          &larr; All Posts
        </Link>
        <h1 className="text-3xl font-bold text-gray-900 mt-2">
          Posts tagged &ldquo;{tag.name}&rdquo;
        </h1>
        <p className="text-gray-500 mt-1">{tag.posts.length} posts</p>
      </div>

      <div className="space-y-6">
        {tag.posts.map((post) => (
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
                {post.tags.map((t) => (
                  <Link
                    key={t.id}
                    href={`/tags/${t.name}`}
                    className={`px-2 py-0.5 rounded text-sm ${
                      t.name === tag.name
                        ? "bg-blue-100 text-blue-700"
                        : "bg-gray-100 text-gray-600 hover:bg-gray-200"
                    }`}
                  >
                    {t.name}
                  </Link>
                ))}
              </div>
            )}
          </article>
        ))}
      </div>
    </div>
  );
}
