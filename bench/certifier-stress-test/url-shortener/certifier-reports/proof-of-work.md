# Proof-of-Work Certification Report

> **Product:** `/Users/yuxuan/work/cc-autonomous/.claude/worktrees/i2p/bench/certifier-stress-test/url-shortener`
> **Intent:** URL shortener with user auth, create short URLs, redirect, click tracking, dashboard with stats, custom short codes, QR codes
> **Generated:** 2026-03-31 02:20:52

## Scores

| Tier | Score | What it measures |
|---|---|---|
| Tier 1 (Endpoints) | 5/20 (25%) | API endpoints exist and respond correctly |
| Tier 2 (Journeys) | 1/10 (10%) | Multi-step user flows complete end-to-end |
| Tier 2 (Steps) | 11/20 (55%) | Individual actions within journeys |

**Verdict:** Not certified: 9 hard failure(s) remain.

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

**link-create**: An authenticated user can create a short URL by providing a destination URL
```
Request:  POST http://localhost:4004/api/links
          Body: {"url": "https://www.example.com/very/long/path/to/resource"}
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:49Z
```

**link-create-custom-code**: An authenticated user can create a short URL with a custom short code
```
Request:  POST http://localhost:4004/api/links
          Body: {"url": "https://www.example.com/custom-destination", "code": "mycode42"}
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:49Z
```

**link-custom-code-unique**: Creating a short URL with a duplicate custom code is rejected
```
Request:  POST http://localhost:4004/api/links
          Body: {"url": "https://www.example.com/first", "code": "dupetest"}
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:49Z
```
```
Request:  POST http://localhost:4004/api/links
          Body: {"url": "https://www.example.com/second", "code": "dupetest"}
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:49Z
```

**link-redirect**: Visiting a short code URL redirects to the original destination URL
```
Request:  POST http://localhost:4004/api/links
          Body: {"url": "https://www.example.com/redirect-target", "code": "redir01"}
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:49Z
```

**click-tracking**: Each redirect increments the click count for the link
```
Request:  POST http://localhost:4004/api/links
          Body: {"url": "https://www.example.com/track-me", "code": "track01"}
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:49Z
```
```
Request:  GET http://localhost:4004/api/links
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:49Z
```
```
Request:  GET http://localhost:4004/api/links
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:49Z
```

**click-details-stored**: Click events record referrer and user agent metadata
```
Request:  POST http://localhost:4004/api/links
          Body: {"url": "https://www.example.com/click-detail", "code": "cdet01"}
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:49Z
```
```
Request:  GET http://localhost:4004/api/links
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:49Z
```
```
Request:  GET http://localhost:4004/api/links
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:49Z
```

**link-list**: An authenticated user can list all their created short URLs
```
Request:  POST http://localhost:4004/api/links
          Body: {"url": "https://www.example.com/listable", "code": "list01"}
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:49Z
```

**link-delete**: An authenticated user can delete their own short URL
```
Request:  POST http://localhost:4004/api/links
          Body: {"url": "https://www.example.com/deletable", "code": "del01"}
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:49Z
```
```
Request:  DELETE http://localhost:4004/api/links/del01
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:50Z
```

**dashboard-shows-stats**: The dashboard displays click count and link details for each short URL
```
Request:  POST http://localhost:4004/api/links
          Body: {"url": "https://www.example.com/dashstats", "code": "dash01"}
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:50Z
```

**qr-code-generation**: A QR code can be generated for a short URL
```
Request:  POST http://localhost:4004/api/links
          Body: {"url": "https://www.example.com/qr-target", "code": "qr01"}
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:50Z
```
```
Request:  GET http://localhost:4004/api/links
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:50Z
```

**link-create-invalid-url**: Creating a short URL with an invalid destination URL is rejected
```
Request:  POST http://localhost:4004/api/links
          Body: {"url": "not-a-valid-url"}
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:50Z
```

**link-ownership-isolation**: A user cannot see or manage links created by another user
```
Request:  POST http://localhost:4004/api/links
          Body: {"url": "https://www.example.com/private-link", "code": "priv01"}
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:50Z
```
```
Request:  GET http://localhost:4004/api/links
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:50Z
```

**link-persistence**: Created short URLs persist and are retrievable after creation
```
Request:  POST http://localhost:4004/api/links
          Body: {"url": "https://www.example.com/persistent", "code": "persist1"}
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:50Z
```

### Passed

**auth-register**: A new user can register with email, name, and password
```
Request:  POST http://localhost:4004/api/register
          Body: {"email": "testuser@example.com", "name": "Test User", "password": "SecurePass123!"}
Response: HTTP 201
          Body: {"id": "cmneepsho0000ooruslrwizag", "email": "testuser@example.com", "name": "Test User"}
Time:     2026-03-31T09:20:48Z
```

**auth-protected-route**: Unauthenticated requests to protected endpoints are rejected
```
Request:  GET http://localhost:4004/api/links
Response: HTTP 401
          Body: {"error": "Unauthorized"}
Time:     2026-03-31T09:20:49Z
```

**dashboard-view**: An authenticated user can view a dashboard showing their links and click stats
```
Request:  GET http://localhost:4004/dashboard
Response: HTTP 200
          Body: "<!DOCTYPE html><html lang=\"en\" class=\"geist_a71539c9-module__T19VSG__variable geist_mono_8d43a2aa-module__8Li5zG__variable h-full antialiased\"><head><meta charSet=\"utf-8\"/><meta name=\"viewport...
Time:     2026-03-31T09:20:50Z
```

**redirect-nonexistent**: Visiting a non-existent short code returns 404
```
Request:  GET http://localhost:4004/nonexistent999
Response: HTTP 404
          Body: {"error": "Not found"}
Time:     2026-03-31T09:20:50Z
```

**auth-register-duplicate**: Registering with an already-used email is rejected
```
Request:  POST http://localhost:4004/api/register
          Body: {"email": "dupe@example.com", "name": "First User", "password": "SecurePass123!"}
Response: HTTP 201
          Body: {"id": "cmneepudm0002oorupzy5ir6d", "email": "dupe@example.com", "name": "First User"}
Time:     2026-03-31T09:20:50Z
```
```
Request:  POST http://localhost:4004/api/register
          Body: {"email": "dupe@example.com", "name": "Second User", "password": "AnotherPass456!"}
Response: HTTP 409
          Body: {"error": "User already exists"}
Time:     2026-03-31T09:20:50Z
```

---

## Tier 2 — User Journey Proof-of-Work

### ✗ Unauthenticated Visitor Follows Short URL (2/3 steps)
_A visitor clicks a short URL and gets redirected to the original destination without needing an account_
**Stopped at:** login

**✓ fresh_session**: new unauthenticated session
_(no proof — step was not an HTTP request)_

**✓ register**: POST /api/register → 201
```
Request:  POST http://localhost:4004/api/register
          Body: {"email": "setup-redirect@test.com", "password": "***", "name": "Setup User"}
Response: HTTP 201
          Body: {"id": "cmneepuij0004ooruwp0jbel1", "email": "setup-redirect@test.com", "name": "Setup User"}
Time:     2026-03-31T02:20:50Z
```

**✗ login**: login failed for setup-redirect@test.com
_(no proof — step was not an HTTP request)_
> ⚠ could not authenticate

### ✗ New User Creates Short URL And Views Dashboard (1/2 steps)
_A new user registers, creates a short URL, and views their dashboard to see the link with click stats_
**Stopped at:** login

**✓ register**: POST /api/register → 201
```
Request:  POST http://localhost:4004/api/register
          Body: {"email": "newuser@test.com", "password": "***", "name": "Test User"}
Response: HTTP 201
          Body: {"id": "cmneepuzd0005ooru6jxhxaut", "email": "newuser@test.com", "name": "Test User"}
Time:     2026-03-31T02:20:51Z
```

**✗ login**: login failed for newuser@test.com
_(no proof — step was not an HTTP request)_
> ⚠ could not authenticate

### ✗ New User Creates Custom Short Code (1/2 steps)
_A new user registers and creates a short URL with a custom vanity code, then verifies it works_
**Stopped at:** login

**✓ register**: POST /api/register → 201
```
Request:  POST http://localhost:4004/api/register
          Body: {"email": "customcode@test.com", "password": "***", "name": "Custom User"}
Response: HTTP 201
          Body: {"id": "cmneepv3b0006ooru2btjdgrq", "email": "customcode@test.com", "name": "Custom User"}
Time:     2026-03-31T02:20:51Z
```

**✗ login**: login failed for customcode@test.com
_(no proof — step was not an HTTP request)_
> ⚠ could not authenticate

### ✗ New User Generates QR Code For Short URL (1/2 steps)
_A new user creates a short URL and generates a QR code for it to share offline_
**Stopped at:** login

**✓ register**: POST /api/register → 201
```
Request:  POST http://localhost:4004/api/register
          Body: {"email": "qruser@test.com", "password": "***", "name": "QR User"}
Response: HTTP 201
          Body: {"id": "cmneepv770007ooru4yfwvyc4", "email": "qruser@test.com", "name": "QR User"}
Time:     2026-03-31T02:20:51Z
```

**✗ login**: login failed for qruser@test.com
_(no proof — step was not an HTTP request)_
> ⚠ could not authenticate

### ✗ Returning User Checks Click Analytics (1/2 steps)
_A returning user logs in to check click statistics on their previously created short URLs_
**Stopped at:** login

**✓ register**: POST /api/register → 201
```
Request:  POST http://localhost:4004/api/register
          Body: {"email": "analytics@test.com", "password": "***", "name": "Analytics User"}
Response: HTTP 201
          Body: {"id": "cmneepvb50008ooru4rfu1fhv", "email": "analytics@test.com", "name": "Analytics User"}
Time:     2026-03-31T02:20:51Z
```

**✗ login**: login failed for analytics@test.com
_(no proof — step was not an HTTP request)_
> ⚠ could not authenticate

### ✗ Returning User Updates And Deletes Short URL (1/2 steps)
_A returning user logs in, updates the destination of an existing short URL, then deletes another one_
**Stopped at:** login

**✓ register**: POST /api/register → 201
```
Request:  POST http://localhost:4004/api/register
          Body: {"email": "manage@test.com", "password": "***", "name": "Manager User"}
Response: HTTP 201
          Body: {"id": "cmneepvf10009oorug475ejxz", "email": "manage@test.com", "name": "Manager User"}
Time:     2026-03-31T02:20:52Z
```

**✗ login**: login failed for manage@test.com
_(no proof — step was not an HTTP request)_
> ⚠ could not authenticate

### ✗ Admin Views All Users And URLs (0/1 steps)
_An admin logs in and views all users' URLs and system-wide statistics_
**Stopped at:** login_admin

**✗ login_admin**: no admin credentials
_(no proof — step was not an HTTP request)_
> ⚠ adapter found no admin user

### ✗ Duplicate Custom Code Rejected (1/2 steps)
_A user tries to create a short URL with a custom code that already exists and gets an appropriate error_
**Stopped at:** login

**✓ register**: POST /api/register → 201
```
Request:  POST http://localhost:4004/api/register
          Body: {"email": "dupetest@test.com", "password": "***", "name": "Dupe Tester"}
Response: HTTP 201
          Body: {"id": "cmneepviy000aoorueq7kwnyn", "email": "dupetest@test.com", "name": "Dupe Tester"}
Time:     2026-03-31T02:20:52Z
```

**✗ login**: login failed for dupetest@test.com
_(no proof — step was not an HTTP request)_
> ⚠ could not authenticate

### ✓ Nonexistent Short Code Returns 404 (2/2 steps)
_A visitor tries to access a short code that doesn't exist and receives a proper error_

**✓ fresh_session**: new unauthenticated session
_(no proof — step was not an HTTP request)_

**✓ GET /api/urls/nonexistent999/redirect**: 404
```
Request:  GET http://localhost:4004/api/urls/nonexistent999/redirect
Response: HTTP 404
          Body: {"text": "<!DOCTYPE html><html lang=\"en\" class=\"geist_a71539c9-module__T19VSG__variable geist_mono_8d43a2aa-module__8Li5zG__variable h-full antialiased\"><head><meta charSet=\"utf-8\"/><meta name=\...
Time:     2026-03-31T02:20:52Z
```

### ✗ URLs Persist Across Sessions (1/2 steps)
_A user creates a short URL, logs out and back in, and confirms the URL still exists with its stats intact_
**Stopped at:** login

**✓ register**: POST /api/register → 201
```
Request:  POST http://localhost:4004/api/register
          Body: {"email": "persist@test.com", "password": "***", "name": "Persist User"}
Response: HTTP 201
          Body: {"id": "cmneepvno000boorum2vtdh7h", "email": "persist@test.com", "name": "Persist User"}
Time:     2026-03-31T02:20:52Z
```

**✗ login**: login failed for persist@test.com
_(no proof — step was not an HTTP request)_
> ⚠ could not authenticate

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
