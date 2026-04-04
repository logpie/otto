import { notFound } from "next/navigation";
import Link from "next/link";
import { prisma } from "@/lib/prisma";

export default async function ProfilePage({
  params,
}: {
  params: { id: string };
}) {
  const user = await prisma.user.findUnique({
    where: { id: params.id },
    include: {
      posts: {
        where: { published: true },
        include: {
          tags: true,
          _count: { select: { likes: true, comments: true } },
        },
        orderBy: { createdAt: "desc" },
      },
      _count: {
        select: { posts: true, comments: true },
      },
    },
  });

  if (!user) notFound();

  return (
    <div>
      <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 mb-8">
        <h1 className="text-2xl font-bold text-gray-900 mb-2">{user.name}</h1>
        <p className="text-gray-500">{user.email}</p>
        <div className="flex gap-6 mt-4 text-sm text-gray-600">
          <span>{user._count.posts} posts</span>
          <span>{user._count.comments} comments</span>
          <span>
            Joined {new Date(user.createdAt).toLocaleDateString()}
          </span>
        </div>
      </div>

      <h2 className="text-xl font-bold text-gray-900 mb-4">Posts</h2>
      <div className="space-y-4">
        {user.posts.map((post) => (
          <article
            key={post.id}
            className="bg-white rounded-lg shadow-sm border border-gray-200 p-6"
          >
            <Link href={`/posts/${post.id}`}>
              <h3 className="text-lg font-semibold text-gray-900 hover:text-blue-600 mb-2">
                {post.title}
              </h3>
            </Link>
            <div className="flex items-center gap-4 text-sm text-gray-500">
              <span>{new Date(post.createdAt).toLocaleDateString()}</span>
              <span>{post._count.likes} likes</span>
              <span>{post._count.comments} comments</span>
            </div>
            {post.tags.length > 0 && (
              <div className="flex gap-2 mt-2">
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
        {user.posts.length === 0 && (
          <p className="text-gray-500">No published posts yet.</p>
        )}
      </div>
    </div>
  );
}
