import Link from "next/link";
import { prisma } from "@/lib/prisma";

export default async function Home() {
  const posts = await prisma.post.findMany({
    where: { published: true },
    include: {
      author: { select: { id: true, name: true } },
      tags: true,
      _count: { select: { likes: true, comments: true } },
    },
    orderBy: { createdAt: "desc" },
    take: 5,
  });

  return (
    <div>
      <section className="text-center py-12">
        <h1 className="text-4xl font-bold text-gray-900 mb-4">
          Welcome to the Blog
        </h1>
        <p className="text-lg text-gray-600 mb-8">
          Discover stories, thinking, and expertise from writers on any topic.
        </p>
        <Link
          href="/posts"
          className="bg-blue-600 text-white px-6 py-2.5 rounded-md hover:bg-blue-700"
        >
          Browse Posts
        </Link>
      </section>

      <section>
        <h2 className="text-2xl font-bold text-gray-900 mb-6">Recent Posts</h2>
        <div className="space-y-6">
          {posts.map((post) => (
            <article
              key={post.id}
              className="bg-white rounded-lg shadow-sm border border-gray-200 p-6"
            >
              <Link href={`/posts/${post.id}`}>
                <h3 className="text-xl font-semibold text-gray-900 hover:text-blue-600 mb-2">
                  {post.title}
                </h3>
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
        </div>
      </section>
    </div>
  );
}
