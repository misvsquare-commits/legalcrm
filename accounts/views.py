"""
Accounts app views — login, logout, profile, user management
"""
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views import View
from django.utils.decorators import method_decorator

from .forms import LoginForm, UserCreateForm, UserProfileForm
from .models import User
from .decorators import admin_required


class LoginView(View):
    template_name = 'registration/login.html'

    def get(self, request):
        if request.user.is_authenticated:
            return self._redirect_by_role(request.user)
        return render(request, self.template_name, {'form': LoginForm()})

    def post(self, request):
        form = LoginForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            messages.success(request, f'Welcome, {user.get_full_name() or user.username}!')
            # Respect ?next= param first, then role-based redirect
            next_url = request.GET.get('next')
            if next_url:
                return redirect(next_url)
            return self._redirect_by_role(user)
        return render(request, self.template_name, {'form': form})

    @staticmethod
    def _redirect_by_role(user):
        """Send each role to the most useful landing page."""
        if user.role == 'client':
            return redirect('client_portal')
        return redirect('dashboard')


def logout_view(request):
    logout(request)
    messages.info(request, 'You have been logged out.')
    return redirect('login')


@method_decorator([login_required, admin_required], name='dispatch')
class UserListView(View):
    template_name = 'accounts/user_list.html'

    def get(self, request):
        users = User.objects.all().order_by('role', 'username')
        return render(request, self.template_name, {'users': users})


@method_decorator([login_required, admin_required], name='dispatch')
class UserCreateView(View):
    template_name = 'accounts/user_form.html'

    def get(self, request):
        return render(request, self.template_name,
                      {'form': UserCreateForm(), 'title': 'Add User'})

    def post(self, request):
        form = UserCreateForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'User created successfully.')
            return redirect('user_list')
        return render(request, self.template_name,
                      {'form': form, 'title': 'Add User'})


@login_required
def profile_view(request):
    if request.method == 'POST':
        form = UserProfileForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Profile updated.')
            return redirect('profile')
    else:
        form = UserProfileForm(instance=request.user)
    return render(request, 'accounts/profile.html', {'form': form})
