"""
This module implements Kiwi TCMS interface to external issue tracking systems.
Refer to each implementor class for integration specifics!
"""

from urllib.parse import urlencode, urlparse

import github
import gitlab
import jira
import redminelib
from django.conf import settings

from tcms.core.contrib.linkreference.models import LinkReference
from tcms.issuetracker.base import IssueTrackerType
from tcms.issuetracker.bugzilla_integration import (  # noqa, pylint: disable=unused-import
    Bugzilla,
)
from tcms.utils.github import repo_id as github_repo_id

# conditional import b/c this App can be disabled
if "tcms.bugs.apps.AppConfig" in settings.INSTALLED_APPS:
    from tcms.issuetracker.kiwitcms import (  # noqa, pylint: disable=unused-import
        KiwiTCMS,
    )


class JIRA(IssueTrackerType):
    """
    Support for JIRA. Requires:

    :base_url: the URL of this JIRA instance. For example https://kiwitcms.atlassian.net
    :api_username: an email address registered with JIRA
    :api_password: API token for this username, see
                   https://id.atlassian.com/manage-profile/security/api-tokens

    .. important::

        The field ``API URL`` is not used for Jira integration and can be left blank!

    Additional control can be applied via the ``JIRA_OPTIONS`` configuration
    setting (in ``product.py``). By default this setting is not provided and
    the code uses ``jira.JIRA.DEFAULT_OPTIONS`` from the ``jira`` Python module!

    .. warning::

        ``TestCase.text`` will be truncated to 30k chars for automated POST
        requests and 6k chars for fallback GET requests to fit inside Jira limitations.
        Otherwise you may see 400, 414 and/or 500 errors!
    """

    def _rpc_connection(self):
        if hasattr(settings, "JIRA_OPTIONS"):
            options = settings.JIRA_OPTIONS
        else:
            options = None

        (api_username, api_password) = self.rpc_credentials

        return jira.JIRA(
            self.bug_system.base_url,
            basic_auth=(api_username, api_password),
            options=options,
        )

    def is_adding_testcase_to_issue_disabled(self):
        (api_username, api_password) = self.rpc_credentials

        return not (self.bug_system.base_url and api_username and api_password)

    @classmethod
    def bug_id_from_url(cls, url):
        """
        Jira IDs are the last group of chars at the end of the URL.
        For example https://issues.jenkins-ci.org/browse/JENKINS-31044 will
        return an ID of JENKINS-31044
        """
        return url.strip().split("/")[-1]

    def details(self, url):
        try:
            issue = self.rpc.issue(self.bug_id_from_url(url))
            return {
                "id": issue.key,
                "description": issue.fields.description,
                "status": issue.fields.status.name,
                "title": issue.fields.summary,
                "url": url,
            }
        except jira.exceptions.JIRAError:
            return super().details(url)

    def get_issue_type_from_jira(self, project_key):
        """
        Returns the issue type from the actual Jira instance.
        Will try to return ``settings.JIRA_ISSUE_TYPE`` if it exists, otherwise will
        return the first found!

        You may override this method if you want more control and customization,
        see https://kiwitcms.org/blog/tags/customization/

        .. versionadded:: 11.4
        """
        try:
            return self.rpc.issue_type_by_name(settings.JIRA_ISSUE_TYPE, project_key)
        except KeyError:
            return self.rpc.issue_types()[0]

    def get_project_from_jira(self, execution):
        """
        Returns the project from the actual Jira instance.
        Will try to match ``execution.run.plan.product.name``, otherwise will
        return the first found!

        You may override this method if you want more control and customization,
        see https://kiwitcms.org/blog/tags/customization/

        .. versionadded:: 11.4
        """
        search_for = execution.build.version.product.name.lower()
        projects_in_jira = self.rpc.projects()
        for project in self.rpc.projects():
            if (project.name.lower() == search_for) or (
                project.key.lower() == search_for
            ):
                return project

        return projects_in_jira[0]

    def _report_issue(self, execution, user):
        """
        JIRA Project == Kiwi TCMS Product, otherwise defaults to the first found
        Issue Type == Bug or the first one found

        If 1-click bug report doesn't work then fall back to manual
        reporting!

        For the HTML API description see:
        https://confluence.atlassian.com/display/JIRA050/Creating+Issues+via+direct+HTML+links
        """

        project = self.get_project_from_jira(execution)
        issue_type = self.get_issue_type_from_jira(project.key)

        try:
            new_issue = self.rpc.create_issue(
                project=project.id,
                issuetype={"name": issue_type.name},
                summary=f"Failed test: {execution.case.summary}",
                description=self._report_comment(execution, user, 30000),
            )
            new_url = self.bug_system.base_url + "/browse/" + new_issue.key

            # add a link reference that will be shown in the UI
            LinkReference.objects.get_or_create(
                execution=execution,
                url=new_url,
                is_defect=True,
            )

            return (new_issue, new_url)
        except jira.exceptions.JIRAError:
            pass

        args = {
            "pid": project.id,
            "issuetype": issue_type.id,
            "summary": f"Failed test: {execution.case.summary}",
            "description": self._report_comment(execution, user, 6000),
        }

        url = self.bug_system.base_url
        if not url.endswith("/"):
            url += "/"

        return (
            None,
            f"{url}secure/CreateIssueDetails!init.jspa?" + urlencode(args, True),
        )

    def post_comment(self, execution, bug_id):
        self.rpc.add_comment(bug_id, self.text(execution))


class GitHub(IssueTrackerType):
    """
    Support for GitHub. Requires:

    :base_url: URL to a GitHub repository for which we're going to report issues
    :api_password: GitHub API token - needs ``repo`` or ``public_repo`` permissions

    .. note::

        You can leave the ``api_url`` and ``api_username`` fields blank because
        the integration code doesn't use them!
    """

    def _rpc_connection(self):
        (_, api_password) = self.rpc_credentials

        # NOTE: we use an access token so only the password field is needed
        return github.Github(auth=github.Auth.Token(api_password))

    def is_adding_testcase_to_issue_disabled(self):
        (_, api_password) = self.rpc_credentials

        return not (self.bug_system.base_url and api_password)

    def _report_issue(self, execution, user):
        """
        GitHub only supports title and body parameters
        """
        args = {
            "title": f"Failed test: {execution.case.summary}",
            "body": self._report_comment(execution, user),
        }

        try:
            repo = self.rpc.get_repo(self.repo_id)
            issue = repo.create_issue(**args)

            # add a link reference that will be shown in the UI
            LinkReference.objects.get_or_create(
                execution=execution,
                url=issue.html_url,
                is_defect=True,
            )

            return (issue, issue.html_url)
        except Exception:  # pylint: disable=broad-except
            # something above didn't work so return a link for manually
            # entering issue details with info pre-filled
            url = self.bug_system.base_url
            if not url.endswith("/"):
                url += "/"

            return (None, url + "/issues/new?" + urlencode(args, True))

    def details(self, url):
        """
        Use GitHub's API instead of OpenGraph to return bug
        details b/c it will work for both public and private URLs.
        """
        repo = self.rpc.get_repo(self.repo_id)
        issue = repo.get_issue(self.bug_id_from_url(url))
        return {
            "id": issue.number,
            "description": issue.body,
            "status": issue.state,
            "title": issue.title,
            "url": url,
        }

    @property
    def repo_id(self):
        return github_repo_id(self.bug_system.base_url)

    def post_comment(self, execution, bug_id):
        repo = self.rpc.get_repo(self.repo_id)

        repo.get_issue(bug_id).create_comment(self.text(execution))


class Gitlab(IssueTrackerType):
    """
    Support for GitLab. Requires:

    :base_url: URL to a GitLab repository for which we're going to report issues.
               For example https://gitlab.com/kiwitcms/integration-testing
    :api_url: URL to a GitLab instance. For example https://gitlab.com
    :api_password: GitLab API token with the ``api`` scope. See
                   https://gitlab.com/-/profile/personal_access_tokens

    .. note::

        You can leave ``api_username`` field blank because
        the integration code doesn't use it!
    """

    def _rpc_connection(self):
        (_, api_password) = self.rpc_credentials

        # we use an access token so only the password field is required
        return gitlab.Gitlab(self.bug_system.api_url, private_token=api_password)

    def is_adding_testcase_to_issue_disabled(self):
        (_, api_password) = self.rpc_credentials

        return not (self.bug_system.api_url and api_password)

    def _report_issue(self, execution, user):
        project = self.rpc.projects.get(self.repo_id)
        new_issue = project.issues.create(
            {
                "title": f"Failed test: {execution.case.summary}",
                "description": self._report_comment(execution, user),
            }
        )

        # and also add a link reference that will be shown in the UI
        LinkReference.objects.get_or_create(
            execution=execution,
            url=new_issue.attributes["web_url"],
            is_defect=True,
        )
        return (new_issue, new_issue.attributes["web_url"])

    def details(self, url):
        """
        Use Gitlab API instead of OpenGraph to return bug
        details b/c it will work for both public and private URLs.
        """
        project = self.rpc.projects.get(self.repo_id)
        issue_id = self.bug_id_from_url(url)
        issue = project.issues.get(issue_id)
        return {
            "id": issue_id,
            "description": issue.description,
            "status": issue.state,
            "title": issue.title,
            "url": url,
        }

    @property
    def repo_id(self):
        return urlparse(self.bug_system.base_url).path.strip("/")

    def post_comment(self, execution, bug_id):
        repo = self.rpc.projects.get(self.repo_id)

        repo.issues.get(bug_id).notes.create({"body": self.text(execution)})


class Redmine(IssueTrackerType):
    """
    Support for Redmine. Requires:

    :base_url: the URL for this Redmine instance. For example http://redmine.example.org:3000
    :api_username: (Optional) A username registered in Redmine. If omitted, the :api_password: field will be treated as an API access token.
    :api_password: Either the password for the Redmine user (when :api_username: is provided), or the API access token (when :api_username: is empty).

    Users can manage or generate their Redmine API tokens by visiting: https://www.redmine.org/my/account → "API access key"
    (or <your-redmine-instance>/my/account if self-hosted)
    """

    def is_adding_testcase_to_issue_disabled(self):
        (api_username, api_password) = self.rpc_credentials

        return not (self.bug_system.base_url and api_password)

    def _rpc_connection(self):
        (api_username, api_password) = self.rpc_credentials

        if api_username:
            connection = redminelib.Redmine(
                self.bug_system.base_url,
                username=api_username,
                password=api_password,
            )
        else:
            connection = redminelib.Redmine(
                self.bug_system.base_url,
                key=self.bug_system.api_access_key
            )

        return connection

    def details(self, url):
        try:
            issue = self.rpc.issue.get(self.bug_id_from_url(url))
            return {
                "id": issue.id,
                "description": issue.description,
                "status": issue.status.name,
                "title": issue.subject,
                "url": url,
            }
        except redminelib.exceptions.ResourceNotFoundError:
            return super().details(url)

    def redmine_project_by_name(self, name):
        """
        Return a Redmine project which matches the given product name.
        Will try to match ``execution.run.plan.product.name``, otherwise will
        return the first found!
        """
        all_projects = self.rpc.project.all()
        for project in all_projects:
            if project.name == name:
                return project

        return all_projects[0]

    @staticmethod
    def redmine_tracker_by_name(project, name):
        """
        Return a Redmine tracker matching name ('Bugs').
        If there is no match then return the first one!
        """
        all_trackers = project.trackers

        for tracker in all_trackers:
            if tracker.name.lower() == name.lower():
                return tracker

        return all_trackers[0]

    def redmine_priority_by_name(self, name):
        all_priorities = self.rpc.enumeration.filter(resource="issue_priorities")

        for priority in all_priorities:
            if priority.name.lower() == name.lower():
                return priority

        return all_priorities[0]

    def _report_issue(self, execution, user):
        project = self.redmine_project_by_name(execution.run.plan.product.name)
        tracker = self.redmine_tracker_by_name(project, settings.REDMINE_TRACKER_NAME)

        # the first Issue Status in Redmine
        status = self.rpc.issue_status.all()[0]

        # try matching TC.priority with IssuePriority in Redmine
        priority = self.redmine_priority_by_name(execution.case.priority.value)

        new_issue = self.rpc.issue.create(
            subject=f"Failed test: {execution.case.summary}",
            description=self._report_comment(execution, user),
            project_id=project.id,
            tracker_id=tracker.id,
            status_id=status.id,
            priority_id=priority.id,
        )
        new_url = f"{self.bug_system.base_url}/issues/{new_issue.id}"

        # and also add a link reference that will be shown in the UI
        LinkReference.objects.get_or_create(
            execution=execution,
            url=new_url,
            is_defect=True,
        )

        return (new_issue, new_url)

    def post_comment(self, execution, bug_id):
        self.rpc.issue.get(bug_id).save(notes=self.text(execution))
