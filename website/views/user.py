import json
import logging
import os
from datetime import datetime, timezone

from allauth.account.signals import user_signed_up
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.contrib.sites.shortcuts import get_current_site
from django.core.mail import send_mail
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import ExtractMonth
from django.dispatch import receiver
from django.http import Http404, HttpResponse, HttpResponseNotFound, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.views.generic import DetailView, ListView, TemplateView, View
from rest_framework.authtoken.models import Token
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.response import Response

from blt import settings
from website.forms import MonitorForm, UserDeleteForm, UserProfileForm
from website.models import (
    IP,
    BaconEarning,
    BaconSubmission,
    Badge,
    Challenge,
    Contributor,
    Domain,
    GitHubIssue,
    Hunt,
    InviteFriend,
    Issue,
    IssueScreenshot,
    Monitor,
    Notification,
    Points,
    Tag,
    Thread,
    User,
    UserBadge,
    UserProfile,
    Wallet,
)

logger = logging.getLogger(__name__)


@receiver(user_signed_up)
def handle_user_signup(request, user, **kwargs):
    referral_token = request.session.get("ref")
    if referral_token:
        try:
            invite = InviteFriend.objects.get(referral_code=referral_token)
            invite.recipients.add(user)
            invite.point_by_referral += 2
            invite.save()
            reward_sender_with_points(invite.sender)
            del request.session["ref"]
        except InviteFriend.DoesNotExist:
            pass


def update_bch_address(request):
    if request.method == "POST":
        selected_crypto = request.POST.get("selected_crypto")
        new_address = request.POST.get("new_address")
        if selected_crypto and new_address:
            try:
                user_profile = request.user.userprofile
                if selected_crypto == "Bitcoin":
                    user_profile.btc_address = new_address
                elif selected_crypto == "Ethereum":
                    user_profile.eth_address = new_address
                elif selected_crypto == "BitcoinCash":
                    user_profile.bch_address = new_address
                else:
                    messages.error(request, f"Invalid crypto selected: {selected_crypto}")
                    return redirect(reverse("profile", args=[request.user.username]))
                user_profile.save()
                messages.success(request, f"{selected_crypto} Address updated successfully.")
            except Exception as e:
                messages.error(request, f"Failed to update {selected_crypto} Address.")
        else:
            messages.error(request, f"Please provide a valid {selected_crypto} Address.")
    else:
        messages.error(request, "Invalid request method.")

        username = request.user.username if request.user.username else "default_username"
        return redirect(reverse("profile", args=[username]))


@login_required
def profile_edit(request):
    Tag.objects.get_or_create(name="GSOC")
    user_profile, created = UserProfile.objects.get_or_create(user=request.user)

    if request.method == "POST":
        form = UserProfileForm(request.POST, request.FILES, instance=user_profile)
        if form.is_valid():
            # Check if email is unique
            new_email = form.cleaned_data["email"]
            if User.objects.exclude(pk=request.user.pk).filter(email=new_email).exists():
                form.add_error("email", "This email is already in use")
                return render(request, "profile_edit.html", {"form": form})

            # Save the form
            form.save()

            # Update the User model's email
            request.user.email = new_email
            request.user.save()

            messages.success(request, "Profile updated successfully!")
            return redirect("profile", slug=request.user.username)
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        # Initialize the form with the user's current email
        form = UserProfileForm(instance=user_profile, initial={"email": request.user.email})

    return render(request, "profile_edit.html", {"form": form})


@login_required(login_url="/accounts/login")
def user_dashboard(request, template="index_user.html"):
    hunts = Hunt.objects.filter(is_published=True)
    upcoming_hunt = []
    ongoing_hunt = []
    previous_hunt = []
    for hunt in hunts:
        if ((hunt.starts_on - datetime.now(timezone.utc)).total_seconds()) > 0:
            upcoming_hunt.append(hunt)
        elif ((hunt.end_on - datetime.now(timezone.utc)).total_seconds()) < 0:
            previous_hunt.append(hunt)
        else:
            ongoing_hunt.append(hunt)
    context = {
        "upcoming_hunts": upcoming_hunt,
        "ongoing_hunt": ongoing_hunt,
        "previous_hunt": previous_hunt,
    }
    return render(request, template, context)


class UserDeleteView(LoginRequiredMixin, View):
    """
    Deletes the currently signed-in user and all associated data.
    """

    def get(self, request, *args, **kwargs):
        form = UserDeleteForm()
        return render(request, "user_deletion.html", {"form": form})

    def post(self, request, *args, **kwargs):
        form = UserDeleteForm(request.POST)
        if form.is_valid():
            user = request.user
            logout(request)
            user.delete()
            messages.success(request, "Account successfully deleted")
            return redirect(reverse("home"))
        return render(request, "user_deletion.html", {"form": form})


class InviteCreate(TemplateView):
    template_name = "invite.html"

    def post(self, request, *args, **kwargs):
        email = request.POST.get("email")
        exists = False
        domain = None
        if email:
            domain = email.split("@")[-1]
        context = {
            "domain": domain,
            "email": email,
        }
        return render(request, "invite.html", context)


def get_github_stats(user_profile):
    # Get all PRs with repo info
    user_prs = (
        GitHubIssue.objects.filter(
            Q(user_profile=user_profile) | Q(reviews__reviewer=user_profile), type="pull_request"
        )
        .select_related("repo")
        .distinct()
        .order_by("-created_at")
    )

    # Calculate reviewed PRs
    reviewed_count = GitHubIssue.objects.filter(
        reviews__reviewer=user_profile,
    ).count()

    print(f"Total PRs found: {user_prs.count()}")

    # Overall stats
    merged_count = user_prs.filter(is_merged=True).count()
    open_count = user_prs.filter(state="open").count()
    closed_count = user_prs.filter(state="closed", is_merged=False).count()

    users_with_github = UserProfile.objects.exclude(github_url="").exclude(github_url=None)
    contributor_count = users_with_github.count()

    # Group PRs by repo
    repos_with_prs = {}
    for pr in user_prs:
        repo_name = pr.repo.name if pr.repo else "Other"
        repo_id = pr.repo.id if pr.repo else None
        repo_url = pr.repo.repo_url if pr.repo else None

        repos_with_prs.setdefault(
            repo_name,
            {
                "repo_name": repo_name,
                "repo_id": repo_id,
                "repo_url": repo_url,
                "pull_requests": [],
                "stats": {"merged_count": 0, "open_count": 0, "closed_count": 0, "reviewed_count": 0},
            },
        )

        repos_with_prs[repo_name]["pull_requests"].append(pr)

        # Track authored vs reviewed contributions
        if pr.user_profile == user_profile:
            if pr.is_merged:
                repos_with_prs[repo_name]["stats"]["merged_count"] += 1
            elif pr.state == "open":
                repos_with_prs[repo_name]["stats"]["open_count"] += 1
            elif pr.state == "closed" and not pr.is_merged:
                repos_with_prs[repo_name]["stats"]["closed_count"] += 1
        elif pr.reviews.filter(reviewer=user_profile).exists():
            repos_with_prs[repo_name]["stats"]["reviewed_count"] += 1

    # Get review ranking
    all_reviewers = (
        UserProfile.objects.annotate(review_count=Count("reviews_made"))
        .filter(review_count__gt=0)
        .order_by("-review_count")
    )

    review_rank = None
    total_reviewers = all_reviewers.count()
    for i, reviewer in enumerate(all_reviewers, 1):
        if reviewer.id == user_profile.id:
            review_rank = f"#{i} out of {total_reviewers}"
            break

    return {
        "overall_stats": {
            "merged_count": merged_count,
            "open_count": open_count,
            "closed_count": closed_count,
            "reviewed_count": reviewed_count,
            "user_rank": f"#{user_profile.contribution_rank} out of {contributor_count}",
            "review_rank": review_rank,
        },
        "repos_with_prs": repos_with_prs,
    }


class UserProfileDetailView(DetailView):
    model = get_user_model()
    slug_field = "username"
    template_name = "profile.html"

    def get(self, request, *args, **kwargs):
        try:
            self.object = self.get_object()
        except Http404:
            messages.error(self.request, "That user was not found.")
            return redirect("/")

        # Update the view count and save the model
        self.object.userprofile.visit_count = len(IP.objects.filter(path=request.path))
        self.object.userprofile.save()

        return super(UserProfileDetailView, self).get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        # if userprofile does not exist, create it
        if not UserProfile.objects.filter(user=self.object).exists():
            UserProfile.objects.create(user=self.object)

        user = self.object
        context = super(UserProfileDetailView, self).get_context_data(**kwargs)
        # Add bacon earning data
        bacon_earning = BaconEarning.objects.filter(user=user).first()
        print(f"Bacon earning for {user.username}: {bacon_earning}")
        context["bacon_earned"] = bacon_earning.tokens_earned if bacon_earning else 0

        # Get bacon submission stats
        context["bacon_submissions"] = {
            "pending": BaconSubmission.objects.filter(user=user, transaction_status="pending").count(),
            "completed": BaconSubmission.objects.filter(user=user, transaction_status="completed").count(),
        }

        milestones = [7, 15, 30, 100, 180, 365]
        base_milestone = 0
        next_milestone = 0
        for milestone in milestones:
            if user.userprofile.current_streak >= milestone:
                base_milestone = milestone
            elif user.userprofile.current_streak < milestone:
                next_milestone = milestone
                break
        context["base_milestone"] = base_milestone
        context["next_milestone"] = next_milestone
        # Fetch badges
        user_badges = UserBadge.objects.filter(user=user).select_related("badge")
        context["user_badges"] = user_badges  # Add badges to context
        context["is_mentor"] = UserBadge.objects.filter(user=user, badge__title="Mentor").exists()
        context["available_badges"] = Badge.objects.all()

        user_points = Points.objects.filter(user=self.object).order_by("id")
        context["user_points"] = user_points
        context["my_score"] = list(user_points.aggregate(total_score=Sum("score")).values())[0]
        context["websites"] = (
            Domain.objects.filter(issue__user=self.object).annotate(total=Count("issue")).order_by("-total")
        )
        context["activities"] = Issue.objects.filter(user=self.object, hunt=None).exclude(
            Q(is_hidden=True) & ~Q(user_id=self.request.user.id)
        )[0:3]
        context["activity_screenshots"] = {}
        for activity in context["activities"]:
            context["activity_screenshots"][activity] = IssueScreenshot.objects.filter(issue=activity.pk).first()
        context["profile_form"] = UserProfileForm()
        context["total_open"] = Issue.objects.filter(user=self.object, status="open").count()
        context["total_closed"] = Issue.objects.filter(user=self.object, status="closed").count()
        context["current_month"] = datetime.now().month
        if self.request.user.is_authenticated:
            context["wallet"] = Wallet.objects.get(user=self.request.user)
        context["graph"] = (
            Issue.objects.filter(user=self.object)
            .filter(
                created__month__gte=(datetime.now().month - 6),
                created__month__lte=datetime.now().month,
            )
            .annotate(month=ExtractMonth("created"))
            .values("month")
            .annotate(c=Count("id"))
            .order_by()
        )
        context["total_bugs"] = Issue.objects.filter(user=self.object, hunt=None).count()
        for i in range(0, 7):
            context["bug_type_" + str(i)] = Issue.objects.filter(user=self.object, hunt=None, label=str(i))

        arr = []
        allFollowers = user.userprofile.follower.all()
        for userprofile in allFollowers:
            arr.append(User.objects.get(username=str(userprofile.user)))
        context["followers"] = arr

        arr = []
        allFollowing = user.userprofile.follows.all()
        for userprofile in allFollowing:
            arr.append(User.objects.get(username=str(userprofile.user)))
        context["following"] = arr

        context["followers_list"] = [str(prof.user.email) for prof in user.userprofile.follower.all()]
        context["bookmarks"] = user.userprofile.issue_saved.all()
        # tags
        context["user_related_tags"] = UserProfile.objects.filter(user=self.object).first().tags.all()
        context["issues_hidden"] = "checked" if user.userprofile.issues_hidden else "!checked"
        # pull request info
        stats = get_github_stats(user.userprofile)
        context.update(
            {
                "overall_stats": stats["overall_stats"],
                "repos_with_prs": stats["repos_with_prs"],
            }
        )

        return context

    @method_decorator(login_required)
    def post(self, request, *args, **kwargs):
        form = UserProfileForm(request.POST, request.FILES, instance=request.user.userprofile)
        if request.FILES.get("user_avatar") and form.is_valid():
            form.save()
        else:
            hide = True if request.POST.get("issues_hidden") == "on" else False
            user_issues = Issue.objects.filter(user=request.user)
            user_issues.update(is_hidden=hide)
            request.user.userprofile.issues_hidden = hide
            request.user.userprofile.save()
        return redirect(reverse("profile", kwargs={"slug": kwargs.get("slug")}))


class LeaderboardBase:
    def get_leaderboard(self, month=None, year=None, api=False):
        data = User.objects

        if year and not month:
            data = data.filter(points__created__year=year)

        if year and month:
            data = data.filter(Q(points__created__year=year) & Q(points__created__month=month))

        data = (
            data.annotate(total_score=Sum("points__score"))
            .order_by("-total_score")
            .filter(
                total_score__gt=0,
            )
        )
        if api:
            return data.values("id", "username", "total_score")

        return data

    def current_month_leaderboard(self, api=False):
        """
        leaderboard which includes current month users scores
        """
        return self.get_leaderboard(month=int(datetime.now().month), year=int(datetime.now().year), api=api)

    def monthly_year_leaderboard(self, year, api=False):
        """
        leaderboard which includes current year top user from each month
        """

        monthly_winner = []

        # iterating over months 1-12
        for month in range(1, 13):
            month_winner = self.get_leaderboard(month, year, api).first()
            monthly_winner.append(month_winner)

        return monthly_winner


class GlobalLeaderboardView(LeaderboardBase, ListView):
    """
    Returns: All users:score data in descending order,
    including pull requests, code reviews, top visitors, and top streakers
    """

    model = User
    template_name = "leaderboard_global.html"

    def get_context_data(self, *args, **kwargs):
        context = super(GlobalLeaderboardView, self).get_context_data(*args, **kwargs)

        user_related_tags = Tag.objects.filter(userprofile__isnull=False).distinct()
        context["user_related_tags"] = user_related_tags

        if self.request.user.is_authenticated:
            context["wallet"] = Wallet.objects.get(user=self.request.user)

        context["leaderboard"] = self.get_leaderboard()[:10]  # Limit to 10 entries

        # Pull Request Leaderboard
        pr_leaderboard = (
            GitHubIssue.objects.filter(type="pull_request", is_merged=True)
            .values(
                "user_profile__user__username",
                "user_profile__user__email",
                "user_profile__github_url",
            )
            .annotate(total_prs=Count("id"))
            .order_by("-total_prs")[:10]
        )
        context["pr_leaderboard"] = pr_leaderboard

        # Reviewed PR Leaderboard
        reviewed_pr_leaderboard = (
            GitHubIssue.objects.filter(type="pull_request")
            .values(
                "reviews__reviewer__user__username",
                "reviews__reviewer__user__email",
                "user_profile__github_url",
            )
            .annotate(total_reviews=Count("id"))
            .order_by("-total_reviews")[:10]
        )
        context["code_review_leaderboard"] = reviewed_pr_leaderboard

        # Top visitors leaderboard
        top_visitors = (
            UserProfile.objects.select_related("user")
            .filter(daily_visit_count__gt=0)
            .order_by("-daily_visit_count")[:10]
        )

        context["top_visitors"] = top_visitors

        return context


class EachmonthLeaderboardView(LeaderboardBase, ListView):
    """
    Returns: Grouped user:score data in months for current year
    """

    model = User
    template_name = "leaderboard_eachmonth.html"

    def get_context_data(self, *args, **kwargs):
        context = super(EachmonthLeaderboardView, self).get_context_data(*args, **kwargs)

        if self.request.user.is_authenticated:
            context["wallet"] = Wallet.objects.get(user=self.request.user)

        year = self.request.GET.get("year")

        if not year:
            year = datetime.now().year

        if isinstance(year, str) and not year.isdigit():
            raise Http404(f"Invalid query passed | Year:{year}")

        year = int(year)

        leaderboard = self.monthly_year_leaderboard(year)
        month_winners = []

        months = [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "Novermber",
            "December",
        ]

        for month_indx, usr in enumerate(leaderboard):
            month_winner = {"user": usr, "month": months[month_indx]}
            month_winners.append(month_winner)

        context["leaderboard"] = month_winners

        return context


class SpecificMonthLeaderboardView(LeaderboardBase, ListView):
    """
    Returns: leaderboard for filtered month and year requested in the query
    """

    model = User
    template_name = "leaderboard_specific_month.html"

    def get_context_data(self, *args, **kwargs):
        context = super(SpecificMonthLeaderboardView, self).get_context_data(*args, **kwargs)

        if self.request.user.is_authenticated:
            context["wallet"] = Wallet.objects.get(user=self.request.user)

        month = self.request.GET.get("month")
        year = self.request.GET.get("year")

        if not month:
            month = datetime.now().month
        if not year:
            year = datetime.now().year

        if isinstance(month, str) and not month.isdigit():
            raise Http404(f"Invalid query passed | Month:{month}")
        if isinstance(year, str) and not year.isdigit():
            raise Http404(f"Invalid query passed | Year:{year}")

        month = int(month)
        year = int(year)

        if not (month >= 1 and month <= 12):
            raise Http404(f"Invalid query passed | Month:{month}")

        context["leaderboard"] = self.get_leaderboard(month, year, api=False)
        return context


class CustomObtainAuthToken(ObtainAuthToken):
    def post(self, request, *args, **kwargs):
        response = super(CustomObtainAuthToken, self).post(request, *args, **kwargs)
        token = Token.objects.get(key=response.data["token"])
        return Response({"key": token.key, "id": token.user_id})


def invite_friend(request):
    if not request.user.is_authenticated:
        return redirect("account_login")
    current_site = get_current_site(request)
    referral_code, created = InviteFriend.objects.get_or_create(sender=request.user)
    referral_link = f"https://{current_site.domain}/referral/?ref={referral_code.referral_code}"
    context = {
        "referral_link": referral_link,
    }
    return render(request, "invite_friend.html", context)


def referral_signup(request):
    referral_token = request.GET.get("ref")
    if referral_token:
        try:
            invite = InviteFriend.objects.get(referral_code=referral_token)
            request.session["ref"] = referral_token
        except InviteFriend.DoesNotExist:
            messages.error(request, "Invalid referral token")
            return redirect("account_signup")
    return redirect("account_signup")


def contributors_view(request, *args, **kwargs):
    contributors_file_path = os.path.join(settings.BASE_DIR, "contributors.json")

    with open(contributors_file_path, "r", encoding="utf-8") as file:
        content = file.read()

    contributors = json.loads(content)

    contributor_id = request.GET.get("contributor", None)

    if contributor_id:
        contributor = None
        for i in contributors:
            if str(i["id"]) == contributor_id:
                contributor = i

        if contributor is None:
            return HttpResponseNotFound("Contributor not found")

        return render(request, "contributors_detail.html", context={"contributor": contributor})

    context = {"contributors": contributors}

    return render(request, "contributors.html", context=context)


def users_view(request, *args, **kwargs):
    context = {}

    # Get total count of users with GitHub profiles
    context["users_with_github_count"] = (
        UserProfile.objects.exclude(github_url="").exclude(github_url__isnull=True).count()
    )

    # Get contributors from database
    context["contributors"] = Contributor.objects.all().order_by("-contributions")
    context["contributors_count"] = context["contributors"].count()

    context["tags_with_counts"] = (
        Tag.objects.filter(userprofile__isnull=False).annotate(user_count=Count("userprofile")).order_by("-user_count")
    )

    tag_name = request.GET.get("tag")
    show_githubbers = request.GET.get("githubbers") == "true"
    show_contributors = request.GET.get("contributors") == "true"

    if show_contributors:
        context["show_contributors"] = True
        context["users"] = []
    elif show_githubbers:
        context["githubbers"] = True
        context["users"] = UserProfile.objects.exclude(github_url="").exclude(github_url__isnull=True)
        context["user_count"] = context["users"].count()
    elif tag_name:
        if context["tags_with_counts"].filter(name=tag_name).exists():
            context["tag"] = tag_name
            context["users"] = UserProfile.objects.filter(tags__name=tag_name)
            context["user_count"] = context["users"].count()
        else:
            context["users"] = UserProfile.objects.none()
            context["user_count"] = 0
    else:
        context["tag"] = "BLT Contributors"
        context["users"] = UserProfile.objects.filter(tags__name="BLT Contributors")
        context["user_count"] = context["users"].count()

    return render(request, "users.html", context=context)


def contributors(request):
    contributors_file_path = os.path.join(settings.BASE_DIR, "contributors.json")

    with open(contributors_file_path, "r", encoding="utf-8", errors="replace") as file:
        content = file.read()

    contributors_data = json.loads(content)
    return JsonResponse({"contributors": contributors_data})


def create_wallet(request):
    for user in User.objects.all():
        Wallet.objects.get_or_create(user=user)
    return JsonResponse("Created", safe=False)


def create_tokens(request):
    for user in User.objects.all():
        Token.objects.get_or_create(user=user)
    return JsonResponse("Created", safe=False)


def get_score(request):
    users = []
    temp_users = (
        User.objects.annotate(total_score=Sum("points__score")).order_by("-total_score").filter(total_score__gt=0)
    )
    rank_user = 1
    for each in temp_users.all():
        temp = {}
        temp["rank"] = rank_user
        temp["id"] = each.id
        temp["User"] = each.username
        temp["score"] = Points.objects.filter(user=each.id).aggregate(total_score=Sum("score"))
        temp["image"] = list(UserProfile.objects.filter(user=each.id).values("user_avatar"))[0]
        temp["title_type"] = list(UserProfile.objects.filter(user=each.id).values("title"))[0]
        temp["follows"] = list(UserProfile.objects.filter(user=each.id).values("follows"))[0]
        temp["savedissue"] = list(UserProfile.objects.filter(user=each.id).values("issue_saved"))[0]
        rank_user = rank_user + 1
        users.append(temp)
    return JsonResponse(users, safe=False)


@login_required(login_url="/accounts/login")
def follow_user(request, user):
    if request.method == "GET":
        try:
            userx = User.objects.get(username=user)
            flag = 0
            list_userfrof = request.user.userprofile.follows.all()
            for prof in list_userfrof:
                if str(prof) == (userx.email):
                    request.user.userprofile.follows.remove(userx.userprofile)
                    flag = 1
            if flag != 1:
                request.user.userprofile.follows.add(userx.userprofile)
                msg_plain = render_to_string("email/follow_user.html", {"follower": request.user, "followed": userx})
                msg_html = render_to_string("email/follow_user.html", {"follower": request.user, "followed": userx})

                send_mail(
                    "You got a new follower!!",
                    msg_plain,
                    settings.EMAIL_TO_STRING,
                    [userx.email],
                    html_message=msg_html,
                )
            return HttpResponse("Success")
        except User.DoesNotExist:
            return HttpResponse(f"User {user} not found", status=404)


# get issue and comment id from url
def monitor_create_view(request):
    if request.method == "POST":
        form = MonitorForm(request.POST)
        if form.is_valid():
            monitor = form.save(commit=False)
            monitor.user = request.user
            monitor.save()
    else:
        form = MonitorForm()
    return render(request, "Moniter.html", {"form": form})


def reward_sender_with_points(sender):
    points, created = Points.objects.get_or_create(user=sender, defaults={"score": 0})
    points.score += 2
    points.save()


@login_required
def deletions(request):
    if request.method == "POST":
        form = MonitorForm(request.POST)
        if form.is_valid():
            monitor = form.save(commit=False)
            monitor.user = request.user
            monitor.save()
            messages.success(request, "Form submitted successfully!")
        else:
            messages.error(request, "Form submission failed. Please correct the errors.")
    else:
        form = MonitorForm()

    return render(
        request,
        "monitor.html",
        {"form": form, "monitors": Monitor.objects.filter(user=request.user)},
    )


def profile(request):
    try:
        return redirect("/profile/" + request.user.username)
    except Exception:
        return redirect("/")


@login_required
def assign_badge(request, username):
    if not UserBadge.objects.filter(user=request.user, badge__title="Mentor").exists():
        messages.error(request, "You don't have permission to assign badges.")
        return redirect("profile", slug=username)

    user = get_object_or_404(get_user_model(), username=username)
    badge_id = request.POST.get("badge")
    reason = request.POST.get("reason", "")
    badge = get_object_or_404(Badge, id=badge_id)

    # Check if the user already has this badge
    if UserBadge.objects.filter(user=user, badge=badge).exists():
        messages.warning(request, "This user already has this badge.")
        return redirect("profile", slug=username)

    # Assign the badge to user
    UserBadge.objects.create(user=user, badge=badge, awarded_by=request.user, reason=reason)
    messages.success(request, f"{badge.title} badge assigned to {user.username}.")
    return redirect("profile", slug=username)


def badge_user_list(request, badge_id):
    badge = get_object_or_404(Badge, id=badge_id)

    profiles = (
        UserProfile.objects.filter(user__userbadge__badge=badge)
        .select_related("user")
        .distinct()
        .annotate(awarded_at=F("user__userbadge__awarded_at"))
        .order_by("-awarded_at")
    )

    return render(
        request,
        "badge_user_list.html",
        {
            "badge": badge,
            "profiles": profiles,
        },
    )


@csrf_exempt
def github_webhook(request):
    if request.method == "POST":
        # Validate GitHub signature
        # this doesn't seem to work?
        # signature = request.headers.get("X-Hub-Signature-256")
        # if not validate_signature(request.body, signature):
        #    return JsonResponse({"status": "error", "message": "Unauthorized request"}, status=403)

        payload = json.loads(request.body)
        event_type = request.headers.get("X-GitHub-Event", "")

        event_handlers = {
            "pull_request": handle_pull_request_event,
            "push": handle_push_event,
            "pull_request_review": handle_review_event,
            "issues": handle_issue_event,
            "status": handle_status_event,
            "fork": handle_fork_event,
            "create": handle_create_event,
        }

        handler = event_handlers.get(event_type)
        if handler:
            return handler(payload)
        else:
            return JsonResponse({"status": "error", "message": "Unhandled event type"}, status=400)
    else:
        return JsonResponse({"status": "error", "message": "Invalid method"}, status=400)


def handle_pull_request_event(payload):
    if payload["action"] == "closed" and payload["pull_request"]["merged"]:
        pr_user_profile = UserProfile.objects.filter(github_url=payload["pull_request"]["user"]["html_url"]).first()
        if pr_user_profile:
            pr_user_instance = pr_user_profile.user
            assign_github_badge(pr_user_instance, "First PR Merged")
    return JsonResponse({"status": "success"}, status=200)


def handle_push_event(payload):
    pusher_profile = UserProfile.objects.filter(github_url=payload["sender"]["html_url"]).first()
    if pusher_profile:
        pusher_user = pusher_profile.user
        if payload.get("commits"):
            assign_github_badge(pusher_user, "First Commit")
    return JsonResponse({"status": "success"}, status=200)


def handle_review_event(payload):
    reviewer_profile = UserProfile.objects.filter(github_url=payload["sender"]["html_url"]).first()
    if reviewer_profile:
        reviewer_user = reviewer_profile.user
        assign_github_badge(reviewer_user, "First Code Review")
    return JsonResponse({"status": "success"}, status=200)


def handle_issue_event(payload):
    print("issue closed")
    if payload["action"] == "closed":
        closer_profile = UserProfile.objects.filter(github_url=payload["sender"]["html_url"]).first()
        if closer_profile:
            closer_user = closer_profile.user
            assign_github_badge(closer_user, "First Issue Closed")
    return JsonResponse({"status": "success"}, status=200)


def handle_status_event(payload):
    user_profile = UserProfile.objects.filter(github_url=payload["sender"]["html_url"]).first()
    if user_profile:
        user = user_profile.user
        build_status = payload["state"]
        if build_status == "success":
            assign_github_badge(user, "First CI Build Passed")
        elif build_status == "failure":
            assign_github_badge(user, "First CI Build Failed")
    return JsonResponse({"status": "success"}, status=200)


def handle_fork_event(payload):
    user_profile = UserProfile.objects.filter(github_url=payload["sender"]["html_url"]).first()
    if user_profile:
        user = user_profile.user
        assign_github_badge(user, "First Fork Created")
    return JsonResponse({"status": "success"}, status=200)


def handle_create_event(payload):
    if payload["ref_type"] == "branch":
        user_profile = UserProfile.objects.filter(github_url=payload["sender"]["html_url"]).first()
        if user_profile:
            user = user_profile.user
            assign_github_badge(user, "First Branch Created")
    return JsonResponse({"status": "success"}, status=200)


def assign_github_badge(user, action_title):
    try:
        badge, created = Badge.objects.get_or_create(title=action_title, type="automatic")
        if not UserBadge.objects.filter(user=user, badge=badge).exists():
            UserBadge.objects.create(user=user, badge=badge)

    except Badge.DoesNotExist:
        print(f"Badge '{action_title}' does not exist.")


@method_decorator(login_required, name="dispatch")
class UserChallengeListView(View):
    """View to display all challenges and handle updates inline."""

    def get(self, request):
        challenges = Challenge.objects.filter(challenge_type="single")  # All single-user challenges
        user_challenges = challenges.filter(participants=request.user)  # Challenges the user is participating in

        for challenge in challenges:
            if challenge in user_challenges:
                # If the user is participating, show their progress
                challenge.progress = challenge.progress
            else:
                # If the user is not participating, set progress to 0
                challenge.progress = 0

            # Calculate the progress circle offset (same as team challenges)
            circumference = 125.6
            challenge.stroke_dasharray = circumference
            challenge.stroke_dashoffset = circumference - (circumference * challenge.progress / 100)

        return render(
            request,
            "user_challenges.html",
            {"challenges": challenges, "user_challenges": user_challenges},
        )


@login_required
def messaging_home(request):
    threads = Thread.objects.filter(participants=request.user).order_by("-updated_at")
    return render(request, "messaging.html", {"threads": threads})


def start_thread(request, user_id):
    if request.method == "POST":
        other_user = get_object_or_404(User, id=user_id)

        # Check if a thread already exists between the two users
        thread = Thread.objects.filter(participants=request.user).filter(participants=other_user).first()

        # Flag if this is a new thread (for sending email)
        is_new_thread = not thread

        if not thread:
            # Create a new thread
            thread = Thread.objects.create()
            thread.participants.set([request.user, other_user])  # Use set() for ManyToManyField

            # Send email notification to the recipient for new thread
            if other_user.email:
                subject = f"New encrypted chat from {request.user.username} on OWASP BLT"
                chat_url = request.build_absolute_uri(reverse("messaging"))

                # Create context for the email template
                context = {
                    "sender_username": request.user.username,
                    "recipient_username": other_user.username,
                    "chat_url": chat_url,
                }

                # Render the email content
                msg_plain = render_to_string("email/new_chat.html", context)
                msg_html = render_to_string("email/new_chat.html", context)

                # Send the email
                send_mail(
                    subject,
                    msg_plain,
                    settings.EMAIL_TO_STRING,
                    [other_user.email],
                    html_message=msg_html,
                )

        return JsonResponse({"success": True, "thread_id": thread.id})

    return JsonResponse({"success": False, "error": "Invalid request"}, status=400)


@login_required
def view_thread(request, thread_id):
    thread = get_object_or_404(Thread, id=thread_id)
    messages = thread.messages.all().order_by("timestamp")
    # Convert the QuerySet to a list of dictionaries using values()
    data = list(messages.values("username", "content", "timestamp"))
    return JsonResponse(data, safe=False)


@login_required
def delete_thread(request, thread_id):
    if request.method == "POST":
        try:
            thread = Thread.objects.get(id=thread_id)
            # Check if user is a participant
            if request.user in thread.participants.all():
                thread.delete()
                return JsonResponse({"status": "success"})
            return JsonResponse({"status": "error", "message": "Unauthorized"}, status=403)
        except Thread.DoesNotExist:
            return JsonResponse({"status": "error", "message": "Thread not found"}, status=404)
    return JsonResponse({"status": "error", "message": "Invalid method"}, status=405)


@login_required
@require_http_methods(["GET"])
def get_public_key(request, thread_id):
    # Get the thread
    thread = get_object_or_404(Thread, id=thread_id)

    # Get the other participant in the thread (exclude the logged-in user)
    other_participants = thread.participants.exclude(id=request.user.id)
    if not other_participants.exists():
        return JsonResponse({"error": "No other participant found"}, status=404)

    other_user = other_participants.first()
    # Access the public_key from the UserProfile
    try:
        public_key = other_user.userprofile.public_key
    except Exception:
        return JsonResponse({"error": "User profile not found"}, status=404)

    if not public_key:
        return JsonResponse({"error": "User has not provided a public key"}, status=404)

    return JsonResponse({"public_key": public_key})


@login_required
@require_http_methods(["POST"])
def set_public_key(request):
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    public_key = data.get("public_key")
    if not public_key:
        return JsonResponse({"error": "Public key is required"}, status=400)

    # Update the public_key on the user's profile
    profile = request.user.userprofile
    profile.public_key = public_key
    profile.save()

    return JsonResponse({"success": True, "public_key": profile.public_key})


@login_required
def fetch_notifications(request):
    notifications = Notification.objects.filter(user=request.user, is_deleted=False).order_by("is_read", "-created_at")

    notifications_data = [
        {
            "id": notification.id,
            "message": notification.message,
            "created_at": notification.created_at,
            "is_read": notification.is_read,
            "notification_type": notification.notification_type,
            "link": notification.link,
        }
        for notification in notifications
    ]

    return JsonResponse({"notifications": notifications_data}, safe=False)


@login_required
def mark_as_read(request):
    if request.method == "PATCH":
        try:
            if request.body and request.content_type == "application/json":
                try:
                    json.loads(request.body)
                except json.JSONDecodeError:
                    return JsonResponse({"status": "error", "message": "Invalid JSON in request body"}, status=400)

            notifications = Notification.objects.filter(user=request.user, is_read=False)
            notifications.update(is_read=True)
            return JsonResponse({"status": "success"})
        except Exception as e:
            logger.error(f"Error marking notifications as read: {e}")
            return JsonResponse(
                {"status": "error", "message": "An error occured while marking notifications as read"}, status=400
            )


@login_required
def delete_notification(request, notification_id):
    if request.method == "DELETE":
        try:
            notification = get_object_or_404(Notification, id=notification_id, user=request.user)

            notification.is_deleted = True
            notification.save()

            return JsonResponse({"status": "success", "message": "Notification deleted successfully"})
        except Exception as e:
            logger.error(f"Error deleting notification: {e}")
            return JsonResponse(
                {"status": "error", "message": "An error occured while deleting notification, please try again."},
                status=400,
            )
    else:
        return JsonResponse({"status": "error", "message": "Invalid request method"}, status=405)
