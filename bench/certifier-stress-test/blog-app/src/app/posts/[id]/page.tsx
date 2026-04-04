import { notFound } from "next/navigation";
import Link from "next/link";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { prisma } from "@/lib/prisma";
import LikeButton from "@/components/LikeButton";
import CommentSection from "@/components/CommentSection";

export default async function PostPage({
  params,
}: {
  params: { id: string };
}) {
  const session = await getServerSession(authOptions);

  const post = await prisma.post.findUnique({
    where: { id: params.id },
    include: {
      author: { select: { id: true, name: true } },
      tags: true,
      comments: {
        include: { author: { select: { id: true, name: true } } },
        orderBy: { createdAt: "desc" },
      },
      likes: true,
      _count: { select: { likes: true } },
    },
  });

  if (!post) notFound();

  if (!post.published && post.authorId !== session?.user?.id) notFound();

  const isAuthor = session?.user?.id === post.authorId;
  const userLiked = session?.user?.id
    ? post.likes.some((like) => like.userId === session.user.id)
    : false;

  return (
    <article>
      <div className="mb-8">
        {!post.published && (
          <span className="inline-block bg-yellow-100 text-yellow-800 text-xs px-2 py-1 rounded mb-3">
            Draft
          </span>
        )}
        <h1 className="text-3xl font-bold text-gray-900 mb-4">{post.title}</h1>
        <div className="flex items-center gap-4 text-sm text-gray-500 mb-4">
          <Link
            href={`/profile/${post.author.id}`}
            className="hover:text-blue-600"
          >
            {post.author.name}
          </Link>
          <span>{new Date(post.createdAt).toLocaleDateString()}</span>
          {isAuthor && (
            <Link
              href={`/posts/${post.id}/edit`}
              className="text-blue-600 hover:underline"
            >
              Edit
            </Link>
          )}
        </div>
        {post.tags.length > 0 && (
          <div className="flex gap-2 mb-6">
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
      </div>

      <div className="mb-8">
        {post.content.split("\n").map((paragraph, i) => (
          <p key={i} className="mb-4 text-gray-700 leading-relaxed">
            {paragraph}
          </p>
        ))}
      </div>

      <div className="border-t border-gray-200 pt-6 mb-8">
        <LikeButton
          postId={post.id}
          initialLiked={userLiked}
          initialCount={post._count.likes}
        />
      </div>

      <CommentSection
        postId={post.id}
        initialComments={post.comments.map((c) => ({
          id: c.id,
          content: c.content,
          authorName: c.author.name,
          authorId: c.author.id,
          createdAt: c.createdAt.toISOString(),
        }))}
        isLoggedIn={!!session?.user}
      />
    </article>
  );
}
