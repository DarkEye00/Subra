from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm

from .models import User


class SignupForm(UserCreationForm):
    class Meta:
        model = User
        fields = ("email",)

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists. Try signing in.")
        return email


class EmailAuthenticationForm(AuthenticationForm):
    username = forms.EmailField(label="Email", widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email"}))

    def clean_username(self):
        return self.cleaned_data["username"].lower()


class InviteSignupForm(UserCreationForm):
    """Sign up from an invitation: the email is the invited one, whatever is posted."""

    class Meta:
        model = User
        fields = ()

    def __init__(self, email, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.invited_email = email.lower()
        self.instance.email = self.invited_email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.invited_email
        if commit:
            user.save()
        return user
