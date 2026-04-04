# Proof-of-Work Certification Report

> **Product:** `/Users/yuxuan/work/cc-autonomous/.claude/worktrees/i2p/bench/certifier-stress-test/blog-app`
> **Intent:** blog platform with user auth CRUD blog posts comments likes tags
> **Generated:** 2026-03-31 15:48:02

## Scores

| Tier | Score | What it measures |
|---|---|---|
| Tier 1 (Endpoints) | 13/22 (59%) | API endpoints exist and respond correctly |
| Tier 2 (Journeys) | 4/8 (50%) | Multi-step user flows complete end-to-end |
| Tier 2 (Steps) | 25/30 (83%) | Individual actions within journeys |

**Verdict:** Not certified: 5 hard failure(s) remain.

---

## How to Read This Report

Every claim below includes **proof**: the exact HTTP request sent,
the exact response received, and when. You can verify any claim by
re-running the request yourself.

```
CLAIM: cart-add-item — Users can add a product to their cart
PROOF:
  Request:  POST http://localhost:3000/api/cart
            Body: {"productId": "abc123", "quantity": 1}
  Response: HTTP 201
            Body: {"id": "cart1", "productId": "abc123", ...}
  Time:     2026-03-31T00:39:50Z
```

If proof is missing, the claim is marked `(no proof)` — the certifier
could not execute the test deterministically.

---

## Tier 1 — Endpoint Proof-of-Work

### Failed

**auth-register**: A new user can register an account with username/email and password
```
Request:  POST http://localhost:4002/api/register
          Body: {"username": "bloguser1", "email": "bloguser1@example.com", "password": "SecurePass123!", "name": "Bound Test User"}
Response: HTTP 400
          Body: {"error": "Email already in use"}
Time:     2026-03-31T22:47:59Z
```

**post-update**: An authenticated user can update their own blog post
```
Request:  PUT http://localhost:4002/api/posts/1
          Body: {"title": "My Updated Blog Post", "content": "Updated content with new information.", "email": "bound-user@eval.local", "name": "Bound Test User", "password": "BoundTest123!"}
Response: HTTP 403
          Body: {"error": "Forbidden"}
Time:     2026-03-31T22:48:00Z
```
```
Request:  GET http://localhost:4002/api/posts
Response: HTTP 200
          Body: [{"id": "cmneekypq001kp04onnjenvcd", "title": "Public Author Article", "content": "This should be visible on my profile", "published": true, "authorId": "cmneekynw001ip04o39ncpf19", "createdAt": "2026...
Time:     2026-03-31T22:48:00Z
```

**post-delete**: An authenticated user can delete their own blog post
```
Request:  POST http://localhost:4002/api/posts
          Body: {"title": "Post To Delete", "content": "This post will be deleted.", "published": false}
Response: HTTP 200
          Body: {"id": "cmnf7juvp0034a9iskkk764ak", "title": "Post To Delete", "content": "This post will be deleted.", "published": false, "authorId": "cmnedb8ri00002t5a44o8dgly", "createdAt": "2026-03-31T22:48:00.2...
Time:     2026-03-31T22:48:00Z
```
```
Request:  DELETE http://localhost:4002/api/posts/{id}
Response: HTTP 403
          Body: {"error": "Forbidden"}
Time:     2026-03-31T22:48:00Z
```
```
Request:  GET http://localhost:4002/api/posts/{id}
Response: HTTP 404
          Body: {"error": "Post not found"}
Time:     2026-03-31T22:48:00Z
```

**comment-create**: An authenticated user can add a comment to a blog post
```
Request:  POST http://localhost:4002/api/posts/1/comments
          Body: {"content": "Great post, very informative!"}
Response: HTTP 500
Time:     2026-03-31T22:48:00Z
```

**comment-delete**: An authenticated user can delete their own comment
```
Request:  DELETE http://localhost:4002/api/posts/cmneekypq001kp04onnjenvcd
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T22:48:00Z
```

**like-post**: An authenticated user can like a blog post
```
Request:  POST http://localhost:4002/api/posts/1/like
Response: HTTP 500
Time:     2026-03-31T22:48:00Z
```
```
Request:  GET http://localhost:4002/api/posts
Response: HTTP 200
          Body: [{"id": "cmneekypq001kp04onnjenvcd", "title": "Public Author Article", "content": "This should be visible on my profile", "published": true, "authorId": "cmneekynw001ip04o39ncpf19", "createdAt": "2026...
Time:     2026-03-31T22:48:00Z
```

**unlike-post**: An authenticated user can unlike a previously liked blog post
```
Request:  DELETE http://localhost:4002/api/posts/cmneekypq001kp04onnjenvcd
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T22:48:00Z
```

**duplicate-like**: Liking a post twice does not create duplicate likes
```
Request:  POST http://localhost:4002/api/posts/1/like
Response: HTTP 500
Time:     2026-03-31T22:48:01Z
```
```
Request:  POST http://localhost:4002/api/posts/1/like
Response: HTTP 500
Time:     2026-03-31T22:48:01Z
```

**post-validation**: Creating a post with missing required fields returns a validation error
```
Request:  POST http://localhost:4002/posts
Response: HTTP 200
          Body: "<!DOCTYPE html><html lang=\"en\"><head><meta charSet=\"utf-8\"/><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/><link rel=\"stylesheet\" href=\"/_next/static/css/app/layout.c...
Time:     2026-03-31T22:48:01Z
```

### Passed

**auth-login**: A registered user can log in and receive an authentication token or session
- Command: `login as alice@example.com`
- Result: NextAuth session established for alice@example.com

**auth-protected-route**: Unauthenticated requests to protected endpoints are rejected
```
Request:  POST http://localhost:4002/api/posts
          Body: {"title": "Unauthorized Post", "content": "This should fail", "published": false}
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T22:47:59Z
```

**post-create**: An authenticated user can create a new blog post with title and content
```
Request:  POST http://localhost:4002/api/posts
          Body: {"title": "My First Blog Post", "content": "This is the body of my first blog post about testing.", "published": false}
Response: HTTP 200
          Body: {"id": "cmnf7juo20032a9isn49adw3u", "title": "My First Blog Post", "content": "This is the body of my first blog post about testing.", "published": false, "authorId": "cmnedb8ri00002t5a44o8dgly", "cre...
Time:     2026-03-31T22:48:00Z
```

**post-list**: Users can retrieve a list of all blog posts
```
Request:  GET http://localhost:4002/api/posts
Response: HTTP 200
          Body: [{"id": "cmneekypq001kp04onnjenvcd", "title": "Public Author Article", "content": "This should be visible on my profile", "published": true, "authorId": "cmneekynw001ip04o39ncpf19", "createdAt": "2026...
Time:     2026-03-31T22:48:00Z
```

**post-read**: Users can retrieve a single blog post by its identifier
```
Request:  GET http://localhost:4002/api/posts
Response: HTTP 200
          Body: [{"id": "cmneekypq001kp04onnjenvcd", "title": "Public Author Article", "content": "This should be visible on my profile", "published": true, "authorId": "cmneekynw001ip04o39ncpf19", "createdAt": "2026...
Time:     2026-03-31T22:48:00Z
```

**comment-list**: Users can view all comments on a blog post
```
Request:  GET http://localhost:4002/api/posts
Response: HTTP 200
          Body: [{"id": "cmneekypq001kp04onnjenvcd", "title": "Public Author Article", "content": "This should be visible on my profile", "published": true, "authorId": "cmneekynw001ip04o39ncpf19", "createdAt": "2026...
Time:     2026-03-31T22:48:00Z
```

**tag-assign**: Users can assign tags to a blog post when creating or updating it
```
Request:  POST http://localhost:4002/api/posts
          Body: {"title": "Tagged Post About JavaScript", "content": "A deep dive into modern JavaScript features.", "tags": ["javascript", "programming"], "published": false}
Response: HTTP 200
          Body: {"id": "cmnf7jvg9003ga9isifqh02kv", "title": "Tagged Post About JavaScript", "content": "A deep dive into modern JavaScript features.", "published": false, "authorId": "cmnedb8ri00002t5a44o8dgly", "cr...
Time:     2026-03-31T22:48:01Z
```

**tag-filter**: Users can filter or list blog posts by tag
```
Request:  GET http://localhost:4002/api/posts?tag=javascript
Response: HTTP 200
          Body: [{"id": "cmneekypq001kp04onnjenvcd", "title": "Public Author Article", "content": "This should be visible on my profile", "published": true, "authorId": "cmneekynw001ip04o39ncpf19", "createdAt": "2026...
Time:     2026-03-31T22:48:01Z
```

**tag-list**: Users can view all available tags
```
Request:  GET http://localhost:4002/api/posts
Response: HTTP 200
          Body: [{"id": "cmneekypq001kp04onnjenvcd", "title": "Public Author Article", "content": "This should be visible on my profile", "published": true, "authorId": "cmneekynw001ip04o39ncpf19", "createdAt": "2026...
Time:     2026-03-31T22:48:01Z
```

**post-ownership**: A user cannot update or delete another user's blog post
```
Request:  PUT http://localhost:4002/api/posts/1
          Body: {"title": "Hijacked Post", "content": "This should not be allowed.", "email": "bound-user@eval.local", "name": "Bound Test User", "password": "BoundTest123!"}
Response: HTTP 403
          Body: {"error": "Forbidden"}
Time:     2026-03-31T22:48:01Z
```
```
Request:  DELETE http://localhost:4002/api/posts/1
Response: HTTP 403
          Body: {"error": "Forbidden"}
Time:     2026-03-31T22:48:01Z
```

**post-persistence**: Blog posts persist across page loads and server restarts
```
Request:  POST http://localhost:4002/api/posts
          Body: {"title": "Persistence Test Post", "content": "This post should survive a page reload.", "published": false}
Response: HTTP 200
          Body: {"id": "cmnf7jvl7003ka9iskxhqp4vm", "title": "Persistence Test Post", "content": "This post should survive a page reload.", "published": false, "authorId": "cmnedb8ri00002t5a44o8dgly", "createdAt": "2...
Time:     2026-03-31T22:48:01Z
```
```
Request:  GET http://localhost:4002/api/posts
Response: HTTP 200
          Body: [{"id": "cmneekypq001kp04onnjenvcd", "title": "Public Author Article", "content": "This should be visible on my profile", "published": true, "authorId": "cmneekynw001ip04o39ncpf19", "createdAt": "2026...
Time:     2026-03-31T22:48:01Z
```

**post-not-found**: Requesting a non-existent blog post returns a 404 error
```
Request:  GET http://localhost:4002/api/posts/99999
Response: HTTP 404
          Body: {"error": "Post not found"}
Time:     2026-03-31T22:48:01Z
```

**browser-homepage**: The blog homepage loads and displays blog posts in a browser
```
Request:  GET http://localhost:4002/
Response: HTTP 200
          Body: "<!DOCTYPE html><html lang=\"en\"><head><meta charSet=\"utf-8\"/><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/><link rel=\"stylesheet\" href=\"/_next/static/css/app/layout.c...
Time:     2026-03-31T22:48:01Z
```

---

## Tier 2 — User Journey Proof-of-Work

### ✓ Unauthenticated Visitor Browses Public Content (3/3 steps)
_A visitor lands on the site and browses any publicly available content without logging in_

**✓ fresh_session**: new unauthenticated session
_(no proof — step was not an HTTP request)_

**✓ GET /**: 200
```
Request:  GET http://localhost:4002/
Response: HTTP 200
          Body: {"text": "<!DOCTYPE html><html lang=\"en\"><head><meta charSet=\"utf-8\"/><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/><link rel=\"stylesheet\" href=\"/_next/static/css/app...
Time:     2026-03-31T15:48:01Z
```

**✓ GET /api/posts**: 200
```
Request:  GET http://localhost:4002/api/posts
Response: HTTP 200
          Body: [{"id": "cmneekypq001kp04onnjenvcd", "title": "Public Author Article", "content": "This should be visible on my profile", "published": true, "authorId": "cmneekynw001ip04o39ncpf19", "createdAt": "2026...
Time:     2026-03-31T15:48:01Z
```

### ✗ New User Registration And First Action (1/2 steps)
_A new user registers an account and performs their first meaningful action in the app_
**Stopped at:** login

**✓ register**: POST /register → 200
```
Request:  POST http://localhost:4002/register
          Body: {"email": "bound-8fa4112941@eval.local", "password": "***", "name": "Bound Test User"}
Response: HTTP 200
          Body: {"text": "<!DOCTYPE html><html lang=\"en\"><head><meta charSet=\"utf-8\"/><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/><link rel=\"stylesheet\" href=\"/_next/static/css/app...
Time:     2026-03-31T15:48:01Z
```

**✗ login**: NextAuth login failed with HTTP 401
```
Request:  POST http://localhost:4002/api/auth/callback/credentials
          Body: {"email": "bound-8fa4112941@eval.local", "password": "***", "csrfToken": "52b41737ba077634fe1f4fb7dc006a52d7002da01b2159a06cd771876e808369", "redirect": "false", "json": "true"}
Response: HTTP 401
          Body: {"url": "http://localhost:4002/api/auth/error?error=CredentialsSignin&provider=credentials"}
Time:     2026-03-31T15:48:01Z
```
> ⚠ NextAuth login failed with HTTP 401

### ✓ Returning User Manages Their Content (6/6 steps)
_A returning user logs in, views their existing content, updates an item, and verifies changes persist_

**✓ register**: POST /register → 200
```
Request:  POST http://localhost:4002/register
          Body: {"email": "bound-db58cea899@eval.local", "password": "***", "name": "Bound Test User"}
Response: HTTP 200
          Body: {"text": "<!DOCTYPE html><html lang=\"en\"><head><meta charSet=\"utf-8\"/><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/><link rel=\"stylesheet\" href=\"/_next/static/css/app...
Time:     2026-03-31T15:48:01Z
```

**✓ login**: NextAuth session active for alice@example.com
```
Request:  POST http://localhost:4002/api/auth/callback/credentials
          Body: {"email": "alice@example.com", "password": "***", "csrfToken": "672e8162585b781b0da724a496cbc38e3bf7f1b34c968486d491d63f8027fc17", "redirect": "false", "json": "true"}
Response: HTTP 200
          Body: {"url": "http://localhost:4002"}
Time:     2026-03-31T15:48:01Z
```

**✓ POST /api/posts**: 200
```
Request:  POST http://localhost:4002/api/posts
          Body: {"title": "Original Title", "content": "Original content here.", "published": false}
Response: HTTP 200
          Body: {"id": "cmnf7jw35003wa9isqkogp2eh", "title": "Original Title", "content": "Original content here.", "published": false, "authorId": "cmnedb8ri00002t5a44o8dgly", "createdAt": "2026-03-31T22:48:01.841Z"...
Time:     2026-03-31T15:48:01Z
```

**✓ PUT /api/posts/cmnf7jw35003wa9isqkogp2eh**: 200
```
Request:  PUT http://localhost:4002/api/posts/cmnf7jw35003wa9isqkogp2eh
          Body: {"title": "Updated Title", "content": "Updated content here.", "published": false}
Response: HTTP 200
          Body: {"id": "cmnf7jw35003wa9isqkogp2eh", "title": "Updated Title", "content": "Updated content here.", "published": false, "authorId": "cmnedb8ri00002t5a44o8dgly", "createdAt": "2026-03-31T22:48:01.841Z", ...
Time:     2026-03-31T15:48:01Z
```

**✓ GET /api/posts/cmnf7jw35003wa9isqkogp2eh**: 200
```
Request:  GET http://localhost:4002/api/posts/cmnf7jw35003wa9isqkogp2eh
Response: HTTP 200
          Body: {"id": "cmnf7jw35003wa9isqkogp2eh", "title": "Updated Title", "content": "Updated content here.", "published": false, "authorId": "cmnedb8ri00002t5a44o8dgly", "createdAt": "2026-03-31T22:48:01.841Z", ...
Time:     2026-03-31T15:48:02Z
```

**✓ verify 'Updated Title' in updated_post**: found
_(no proof — step was not an HTTP request)_

### ✗ Full CRUD Lifecycle (1/2 steps)
_A user creates, reads, updates, and deletes content to verify the complete data lifecycle_
**Stopped at:** login

**✓ register**: POST /register → 200
```
Request:  POST http://localhost:4002/register
          Body: {"email": "bound-f92c4b4156@eval.local", "password": "***", "name": "Bound Test User"}
Response: HTTP 200
          Body: {"text": "<!DOCTYPE html><html lang=\"en\"><head><meta charSet=\"utf-8\"/><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/><link rel=\"stylesheet\" href=\"/_next/static/css/app...
Time:     2026-03-31T15:48:02Z
```

**✗ login**: NextAuth login failed with HTTP 401
```
Request:  POST http://localhost:4002/api/auth/callback/credentials
          Body: {"email": "bound-f92c4b4156@eval.local", "password": "***", "csrfToken": "440d26f90d76aad2677773888eef91e4a0b5833aef38b728df48ee9a08e26908", "redirect": "false", "json": "true"}
Response: HTTP 401
          Body: {"url": "http://localhost:4002/api/auth/error?error=CredentialsSignin&provider=credentials"}
Time:     2026-03-31T15:48:02Z
```
> ⚠ NextAuth login failed with HTTP 401

### ✗ Data Persistence Across Sessions (6/7 steps)
_A user creates content, logs out, logs back in, and verifies their data is still there_
**Stopped at:** 6/7 steps passed

**✓ register**: POST /register → 200
```
Request:  POST http://localhost:4002/register
          Body: {"email": "bound-e5643a7d02@eval.local", "password": "***", "name": "Bound Test User"}
Response: HTTP 200
          Body: {"text": "<!DOCTYPE html><html lang=\"en\"><head><meta charSet=\"utf-8\"/><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/><link rel=\"stylesheet\" href=\"/_next/static/css/app...
Time:     2026-03-31T15:48:02Z
```

**✓ login**: NextAuth session active for alice@example.com
```
Request:  POST http://localhost:4002/api/auth/callback/credentials
          Body: {"email": "alice@example.com", "password": "***", "csrfToken": "a708f179ce16e309234ebcfde6c0152df15b920e54ab33019d55b69dcfb90c37", "redirect": "false", "json": "true"}
Response: HTTP 200
          Body: {"url": "http://localhost:4002"}
Time:     2026-03-31T15:48:02Z
```

**✓ POST /api/posts**: 200
```
Request:  POST http://localhost:4002/api/posts
          Body: {"title": "Persistent Data", "content": "This should survive logout.", "published": false}
Response: HTTP 200
          Body: {"id": "cmnf7jwdx003ya9ise5rxna6q", "title": "Persistent Data", "content": "This should survive logout.", "published": false, "authorId": "cmnedb8ri00002t5a44o8dgly", "createdAt": "2026-03-31T22:48:02...
Time:     2026-03-31T15:48:02Z
```

**✓ fresh_session**: new unauthenticated session
_(no proof — step was not an HTTP request)_

**✓ login**: NextAuth session active for alice@example.com
```
Request:  POST http://localhost:4002/api/auth/callback/credentials
          Body: {"email": "alice@example.com", "password": "***", "csrfToken": "f555d9c0bf8c5f0112b4bc9042c9ad95b07fbc16009f8c5b7d6498914488abb1", "redirect": "false", "json": "true"}
Response: HTTP 200
          Body: {"url": "http://localhost:4002"}
Time:     2026-03-31T15:48:02Z
```

**✓ GET /api/posts**: 200
```
Request:  GET http://localhost:4002/api/posts
Response: HTTP 200
          Body: [{"id": "cmneekypq001kp04onnjenvcd", "title": "Public Author Article", "content": "This should be visible on my profile", "published": true, "authorId": "cmneekynw001ip04o39ncpf19", "createdAt": "2026...
Time:     2026-03-31T15:48:02Z
```

**✗ verify 'Persistent Data' in after_relogin**: NOT found
_(no proof — step was not an HTTP request)_
> ⚠ 'Persistent Data' not in after_relogin

### ✗ Admin Management Operations (2/4 steps)
_An admin user logs in and performs management actions like viewing all users or all content_
**Stopped at:** 2/4 steps passed

**✓ login_admin**: NextAuth session active for alice@example.com
```
Request:  POST http://localhost:4002/api/auth/callback/credentials
          Body: {"email": "alice@example.com", "password": "***", "csrfToken": "e4f5f710c42bc876dc24ed0a86e9dda62d5886252e4dc6da3452ef9c07f8d89c", "redirect": "false", "json": "true"}
Response: HTTP 200
          Body: {"url": "http://localhost:4002"}
Time:     2026-03-31T15:48:02Z
```

**✗ GET /admin/users**: HTTP 404
```
Request:  GET http://localhost:4002/admin/users
Response: HTTP 404
          Body: {"text": "<!DOCTYPE html><html lang=\"en\"><head><meta charSet=\"utf-8\"/><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/><link rel=\"stylesheet\" href=\"/_next/static/css/app...
Time:     2026-03-31T15:48:02Z
```
> ⚠ expected [200], got 404

**✗ verify all_users has ≥1 items**: count=0
_(no proof — step was not an HTTP request)_
> ⚠ expected ≥1, got 0

**✓ GET /api/posts**: 200
```
Request:  GET http://localhost:4002/api/posts
Response: HTTP 200
          Body: [{"id": "cmneekypq001kp04onnjenvcd", "title": "Public Author Article", "content": "This should be visible on my profile", "published": true, "authorId": "cmneekynw001ip04o39ncpf19", "createdAt": "2026...
Time:     2026-03-31T15:48:02Z
```

### ✓ Unauthorized Access Denied (3/3 steps)
_An unauthenticated user tries to access protected endpoints and is rejected_

**✓ fresh_session**: new unauthenticated session
_(no proof — step was not an HTTP request)_

**✓ POST /api/posts**: 401
```
Request:  POST http://localhost:4002/api/posts
          Body: {"title": "Sneaky Post", "content": "Should be rejected.", "published": false}
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T15:48:02Z
```

**✓ DELETE /api/posts/1**: 401
```
Request:  DELETE http://localhost:4002/api/posts/1
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T15:48:02Z
```

### ✓ Duplicate Registration Rejected (3/3 steps)
_A user tries to register with an email that already exists and gets an appropriate error_

**✓ register**: POST /register → 200
```
Request:  POST http://localhost:4002/register
          Body: {"email": "bound-54974292c5@eval.local", "password": "***", "name": "Bound Test User"}
Response: HTTP 200
          Body: {"text": "<!DOCTYPE html><html lang=\"en\"><head><meta charSet=\"utf-8\"/><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/><link rel=\"stylesheet\" href=\"/_next/static/css/app...
Time:     2026-03-31T15:48:02Z
```

**✓ register**: POST /register → 200
```
Request:  POST http://localhost:4002/register
          Body: {"email": "bound-54974292c5@eval.local", "password": "***", "name": "Bound Test User"}
Response: HTTP 200
          Body: {"text": "<!DOCTYPE html><html lang=\"en\"><head><meta charSet=\"utf-8\"/><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/><link rel=\"stylesheet\" href=\"/_next/static/css/app...
Time:     2026-03-31T15:48:02Z
```

**✓ GET /**: 200
```
Request:  GET http://localhost:4002/
Response: HTTP 200
          Body: {"text": "<!DOCTYPE html><html lang=\"en\"><head><meta charSet=\"utf-8\"/><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/><link rel=\"stylesheet\" href=\"/_next/static/css/app...
Time:     2026-03-31T15:48:02Z
```

---

## Scope & Limitations

**What this report proves:**
- Every claim was tested with a real HTTP request
- Responses were received and validated
- Timestamps are included for auditability

**What this report does NOT prove:**
- Visual rendering (no screenshots in Tier 1/2 sequential mode)
- Real payment processing (Stripe uses placeholder keys)
- Performance or load handling
- Accessibility or mobile responsiveness
- Security beyond basic auth checks

To verify any claim, re-run the documented request against the
running application and compare the response.
