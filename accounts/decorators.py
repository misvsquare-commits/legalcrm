"""
Custom decorators for Role-Based Access Control (RBAC)
"""
from django.shortcuts import redirect
from django.contrib import messages
from functools import wraps


def admin_required(view_func):
    """Restrict view to admin users only"""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if hasattr(request, 'user') and request.user.is_authenticated:
            if request.user.role == 'admin' or request.user.is_superuser:
                return view_func(request, *args, **kwargs)
        messages.error(request, 'You do not have permission to access this page.')
        return redirect('dashboard')
    return wrapper


def lawyer_or_admin_required(view_func):
    """Restrict view to lawyers and admins"""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if hasattr(request, 'user') and request.user.is_authenticated:
            if request.user.role in ('admin', 'lawyer') or request.user.is_superuser:
                return view_func(request, *args, **kwargs)
        messages.error(request, 'You do not have permission to perform this action.')
        return redirect('dashboard')
    return wrapper
