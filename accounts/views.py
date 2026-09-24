from django.contrib.auth import login
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from .forms import SignupForm


def signup(request):
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            if next_url and url_has_allowed_host_and_scheme(next_url, {request.get_host()}, request.is_secure()):
                return redirect(next_url)
            return redirect("home")
    else:
        form = SignupForm()
    return render(request, "accounts/signup.html", {"form": form, "next": next_url})
