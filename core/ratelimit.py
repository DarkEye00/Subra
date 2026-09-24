from django.core.cache import cache


def client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR", "")


def allow(request, action, limit, window):
    """Fixed-window limit per user and per IP. Returns False once either is over the limit."""
    keys = [f"rl:{action}:ip:{client_ip(request)}"]
    if request.user.is_authenticated:
        keys.append(f"rl:{action}:user:{request.user.pk}")
    allowed = True
    for key in keys:
        cache.add(key, 0, window)
        try:
            count = cache.incr(key)
        except ValueError:  # expired between add and incr
            cache.set(key, 1, window)
            count = 1
        allowed = allowed and count <= limit
    return allowed
