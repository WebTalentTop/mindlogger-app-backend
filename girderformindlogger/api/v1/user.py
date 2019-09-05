# -*- coding: utf-8 -*-
import base64
import cherrypy
import datetime
import itertools

from ..describe import Description, autoDescribeRoute
from girderformindlogger.api import access
from girderformindlogger.api.rest import Resource, filtermodel, setCurrentUser
from girderformindlogger.constants import AccessType, SortDir, TokenScope, USER_ROLES
from girderformindlogger.exceptions import RestException, AccessException
from girderformindlogger.models.applet import Applet as AppletModel
from girderformindlogger.models.collection import Collection as CollectionModel
from girderformindlogger.models.folder import Folder as FolderModel
from girderformindlogger.models.group import Group as GroupModel
from girderformindlogger.models.setting import Setting
from girderformindlogger.models.token import Token
from girderformindlogger.models.user import User as UserModel
from girderformindlogger.settings import SettingKey
from girderformindlogger.utility import jsonld_expander, mail_utils
from sys import exc_info


class User(Resource):
    """API Endpoint for users in the system."""

    def __init__(self):
        super(User, self).__init__()
        self.resourceName = 'user'
        self._model = UserModel()

        self.route('DELETE', ('authentication',), self.logout)
        self.route('DELETE', (':id',), self.deleteUser)
        self.route('GET', (), self.find)
        self.route('GET', ('me',), self.getMe)
        self.route('GET', ('authentication',), self.login)
        self.route('GET', (':id',), self.getUser)
        self.route('GET', (':id', 'access'), self.getUserAccess)
        self.route('PUT', (':id', 'access'), self.updateUserAccess)
        self.route('GET', (':id', 'applets'), self.getUserApplets)
        self.route('GET', (':id', 'details'), self.getUserDetails)
        self.route('GET', ('invites',), self.getGroupInvites)
        self.route('GET', ('details',), self.getUsersDetails)
        self.route('POST', (), self.createUser)
        self.route('PUT', (':id',), self.updateUser)
        self.route('PUT', ('password',), self.changePassword)
        self.route('PUT', (':id', 'password'), self.changeUserPassword)
        self.route('GET', ('password', 'temporary', ':id'),
                   self.checkTemporaryPassword)
        self.route('PUT', ('password', 'temporary'),
                   self.generateTemporaryPassword)
        self.route('POST', (':id', 'otp'), self.initializeOtp)
        self.route('PUT', (':id', 'otp'), self.finalizeOtp)
        self.route('DELETE', (':id', 'otp'), self.removeOtp)
        self.route('PUT', (':id', 'verification'), self.verifyEmail)
        self.route('POST', ('verification',), self.sendVerificationEmail)


    @access.user
    @autoDescribeRoute(
        Description('Get all pending invites for the logged-in user.')
    )
    def getGroupInvites(self):
        pending = self.getCurrentUser().get("groupInvites")
        output = []
        userfields = [
            'firstName',
            'lastName',
            '_id',
            'email',
            'gravatar_baseUrl',
            'login'
        ]
        for p in pending:
            groupId = p.get('groupId')
            applets = list(AppletModel().find(
                query={
                    "roles.user.groups.id": groupId
                },
                fields=[
                    'cached.applet.skos:prefLabel',
                    'cached.applet.schema:description',
                    'cached.applet.schema:image',
                    'roles'
                ]
            ))
            for applet in applets:
                for role in ['manager', 'reviewer']:
                    applet[''.join([role, 's'])] = [{
                        (
                            'image' if userKey=='gravatar_baseUrl' else userKey
                        ): user.get(
                            userKey
                        ) for userKey in user.keys()
                    } for user in list(UserModel().find(
                            query={
                                "groups": {
                                    "$in": [
                                        group.get('id') for group in applet.get(
                                            'roles',
                                            {}
                                        ).get(role, {}).get('groups', [])
                                    ]
                                }
                            },
                            fields=userfields
                        ))
                    ]
            output.append({
                '_id': groupId,
                'applets': [{
                    'name': applet.get('cached', {}).get('applet', {}).get(
                        'skos:prefLabel',
                        ''
                    ),
                    'image': applet.get('cached', {}).get('applet', {}).get(
                        'schema:image',
                        ''
                    ),
                    'description': applet.get('cached', {}).get('applet', {
                    }).get(
                        'schema:description',
                        ''
                    ),
                    'managers': applet.get('managers'),
                    'reviewers': applet.get('reviewers')
                } for applet in applets]
            })
        return(output)

    @access.user
    @filtermodel(model=UserModel)
    @autoDescribeRoute(
        Description('List or search for users.')
        .responseClass('User', array=True)
        .param('text', 'Pass this to perform a full text search for items.', required=False)
        .pagingParams(defaultSort='lastName')
    )
    def find(self, text, limit, offset, sort):
        return list(self._model.search(
            text=text, user=self.getCurrentUser(), offset=offset, limit=limit, sort=sort))

    @access.public(scope=TokenScope.USER_INFO_READ)
    @filtermodel(model=UserModel)
    @autoDescribeRoute(
        Description('Get a user by ID.')
        .responseClass('User')
        .modelParam('id', model=UserModel, level=AccessType.READ)
        .errorResponse('ID was invalid.')
        .errorResponse('You do not have permission to see this user.', 403)
    )
    def getUser(self, user):
        return user

    @access.user(scope=TokenScope.USER_INFO_READ)
    @autoDescribeRoute(
        Description('Get the access control list for a user.')
        .responseClass('User')
        .modelParam('id', model=UserModel, level=AccessType.READ)
        .errorResponse('ID was invalid.')
        .errorResponse('You do not have permission to see this user.', 403)
    )
    def getUserAccess(self, user):
        return self._model.getFullAccessList(user)

    @access.user(scope=TokenScope.DATA_OWN)
    @filtermodel(model=UserModel, addFields={'access'})
    @autoDescribeRoute(
        Description('Update the access control list for a user.')
        .modelParam('id', model=UserModel, level=AccessType.WRITE)
        .jsonParam(
            'access',
            'The JSON-encoded access control list.',
            requireObject=True
        )
        .errorResponse('ID was invalid.')
        .errorResponse('Admin access was denied for the user.', 403)
    )
    def updateUserAccess(self, user, access):
        return self._model.setAccessList(
            user,
            access,
            save=True
        )

    @access.public(scope=TokenScope.DATA_READ)
    @autoDescribeRoute(
        Description('Get all applets for a user by that user\'s ID and role.')
        .modelParam('id', model=UserModel, level=AccessType.READ)
        .param(
            'role',
            'One of ' + str(USER_ROLES.keys()),
            required=False,
            default='user'
        )
        .param(
            'ids_only',
            'If true, only returns an Array of the IDs of assigned applets. '
            'Otherwise, returns an Array of Objects keyed with "applet" '
            '"activitySet", "activities" and "items" with expanded JSON-LD as '
            'values.',
            required=False,
            default=False,
            dataType='boolean'
        )
        .errorResponse('ID was invalid.')
        .errorResponse(
            'You do not have permission to see any of this user\'s applets.',
            403
        )
    )
    def getUserApplets(self, user, role, ids_only):
        from bson.objectid import ObjectId
        user = user if user else self.getCurrentUser()
        role = role.lower()
        if role not in USER_ROLES.keys():
            raise RestException(
                'Invalid user role.',
                'role'
            )
        reviewer = self.getCurrentUser()
        # New schema, new roles
        applets = AppletModel().getAppletsForUser(role, user, active=True)
        if len(applets)==0:
            return([])
        if ids_only==True:
            return([applet.get('_id') for applet in applets])
        try:
            return(
                [
                    {
                        **jsonld_expander.formatLdObject(
                            applet,
                            'applet',
                            reviewer,
                            refreshCache=False
                        ),
                        "groups": AppletModel().getAppletGroups(
                            applet,
                            arrayOfObjects=True
                        )
                    } if role=="manager" else {
                        **jsonld_expander.formatLdObject(
                            applet,
                            'applet',
                            reviewer,
                            dropErrors=True
                        ),
                        "groups": [
                            group for group in AppletModel(
                            ).getAppletGroups(applet).get(role) if ObjectId(
                                group
                            ) in [
                                *user.get('groups', []),
                                *user.get('formerGroups', []),
                                *[invite['groupId'] for invite in [
                                    *user.get('groupInvites', []),
                                    *user.get('declinedInvites', [])
                                ]]
                            ]
                        ]
                    } for applet in applets if (
                        applet is not None and not applet.get(
                            'meta',
                            {}
                        ).get(
                            'applet',
                            {}
                        ).get('deleted')
                    )
                ]
            )
        except Exception as e:
            return(e)


    @access.public(scope=TokenScope.USER_INFO_READ)
    @filtermodel(model=UserModel)
    @autoDescribeRoute(
        Description('Retrieve the currently logged-in user information.')
        .responseClass('User')
    )
    def getMe(self):
        return self.getCurrentUser()

    @access.public
    @autoDescribeRoute(
        Description('Log in to the system.')
        .notes('Pass your username and password using HTTP Basic Auth. Sends'
               ' a cookie that should be passed back in future requests.')
        .param('Girder-OTP', 'A one-time password for this user', paramType='header',
               required=False)
        .errorResponse('Missing Authorization header.', 401)
        .errorResponse('Invalid login or password.', 403)
    )
    def login(self):
        if not Setting().get(SettingKey.ENABLE_PASSWORD_LOGIN):
            raise RestException('Password login is disabled on this instance.')

        user, token = self.getCurrentUser(returnToken=True)

        # Only create and send new cookie if user isn't already sending a valid one.
        if not user:
            authHeader = cherrypy.request.headers.get('Authorization')

            if not authHeader:
                authHeader = cherrypy.request.headers.get('Girder-Authorization')

            if not authHeader or not authHeader[0:6] == 'Basic ':
                raise RestException('Use HTTP Basic Authentication', 401)

            try:
                credentials = base64.b64decode(authHeader[6:]).decode('utf8')
                if ':' not in credentials:
                    raise TypeError
            except Exception:
                raise RestException('Invalid HTTP Authorization header', 401)

            login, password = credentials.split(':', 1)
            otpToken = cherrypy.request.headers.get('Girder-OTP')
            user = self._model.authenticate(login, password, otpToken)

            setCurrentUser(user)
            token = self.sendAuthTokenCookie(user)

        return {
            'user': self._model.filter(user, user),
            'authToken': {
                'token': token['_id'],
                'expires': token['expires'],
                'scope': token['scope']
            },
            'message': 'Login succeeded.'
        }

    @access.public
    @autoDescribeRoute(
        Description('Log out of the system.')
        .responseClass('Token')
        .notes('Attempts to delete your authentication cookie.')
    )
    def logout(self):
        token = self.getCurrentToken()
        if token:
            Token().remove(token)
        self.deleteAuthTokenCookie()
        return {'message': 'Logged out.'}

    @access.public
    @filtermodel(model=UserModel, addFields={'authToken'})
    @autoDescribeRoute(
        Description('Create a new user.')
        .responseClass('User')
        .param('login', "The user's requested login.")
        .param('email', "The user's email address.")
        .param('firstName', "The user's first name.")
        .param('lastName', "The user's last name.")
        .param('password', "The user's requested password")
        .param('admin', 'Whether this user should be a site administrator.',
               required=False, dataType='boolean', default=False)
        .errorResponse('A parameter was invalid, or the specified login or'
                       ' email already exists in the system.')
    )
    def createUser(self, login, email, firstName, lastName, password, admin):
        currentUser = self.getCurrentUser()

        regPolicy = Setting().get(SettingKey.REGISTRATION_POLICY)

        if not currentUser or not currentUser['admin']:
            admin = False
            if regPolicy == 'closed':
                raise RestException(
                    'Registration on this instance is closed. Contact an '
                    'administrator to create an account for you.')

        user = self._model.createUser(
            login=login, password=password, email=email,
            firstName=firstName if firstName is not None else "",
            lastName=lastName, admin=admin, currentUser=currentUser)

        if not currentUser and self._model.canLogin(user):
            setCurrentUser(user)
            token = self.sendAuthTokenCookie(user)
            user['authToken'] = {
                'token': token['_id'],
                'expires': token['expires']
            }

        # Assign all new users to a "New Users" Group
        newUserGroup = GroupModel().findOne({'name': 'New Users'})
        newUserGroup = newUserGroup if (
            newUserGroup is not None and bool(newUserGroup)
        ) else GroupModel(
        ).createGroup(
            name="New Users",
            creator=UserModel().findOne(
                query={'admin': True},
                sort=[('created', SortDir.ASCENDING)]
            ),
            public=False
        )
        group = GroupModel().addUser(
            newUserGroup,
            user,
            level=AccessType.READ
        )
        group['access'] = GroupModel().getFullAccessList(group)
        group['requests'] = list(GroupModel().getFullRequestList(group))

        return(user)

    @access.user
    @autoDescribeRoute(
        Description('Delete a user by ID.')
        .modelParam('id', model=UserModel, level=AccessType.ADMIN)
        .errorResponse('ID was invalid.')
        .errorResponse('You do not have permission to delete this user.', 403)
    )
    def deleteUser(self, user):
        self._model.remove(user)
        return {'message': 'Deleted user %s.' % user['login']}

    @access.user
    @autoDescribeRoute(
        Description('Get detailed information of accessible users.')
    )
    def getUsersDetails(self):
        nUsers = self._model.findWithPermissions(user=self.getCurrentUser()).count()
        return {'nUsers': nUsers}

    @access.user
    @filtermodel(model=UserModel)
    @autoDescribeRoute(
        Description("Update a user's information.")
        .modelParam('id', model=UserModel, level=AccessType.WRITE)
        .param('firstName', 'First name of the user.')
        .param('lastName', 'Last name of the user.')
        .param('email', 'The email of the user.')
        .param('admin', 'Is the user a site admin (admin access required)',
               required=False, dataType='boolean')
        .param('status', 'The account status (admin access required)',
               required=False, enum=('pending', 'enabled', 'disabled'))
        .errorResponse()
        .errorResponse(('You do not have write access for this user.',
                        'Must be an admin to create an admin.'), 403)
    )
    def updateUser(self, user, firstName, lastName, email, admin, status):
        user['firstName'] = firstName
        user['lastName'] = lastName
        user['email'] = email

        # Only admins can change admin state
        if admin is not None:
            if self.getCurrentUser()['admin']:
                user['admin'] = admin
            elif user['admin'] is not admin:
                raise AccessException('Only admins may change admin status.')

        # Only admins can change status
        if status is not None and status != user.get('status', 'enabled'):
            if not self.getCurrentUser()['admin']:
                raise AccessException('Only admins may change status.')
            if user['status'] == 'pending' and status == 'enabled':
                # Send email on the 'pending' -> 'enabled' transition
                self._model._sendApprovedEmail(user)
            user['status'] = status

        return self._model.save(user)

    @access.admin
    @autoDescribeRoute(
        Description("Change a user's password.")
        .notes('Only administrators may use this endpoint.')
        .modelParam('id', model=UserModel, level=AccessType.ADMIN)
        .param('password', "The user's new password.")
        .errorResponse('You are not an administrator.', 403)
        .errorResponse('The new password is invalid.')
    )
    def changeUserPassword(self, user, password):
        self._model.setPassword(user, password)
        return {'message': 'Password changed.'}

    @access.user
    @autoDescribeRoute(
        Description('Change your password.')
        .param('old', 'Your current password or a temporary access token.')
        .param('new', 'Your new password.')
        .errorResponse(('You are not logged in.',
                        'Your old password is incorrect.'), 401)
        .errorResponse('Your new password is invalid.')
    )
    def changePassword(self, old, new):
        user = self.getCurrentUser()
        token = None

        if not old:
            raise RestException('Old password must not be empty.')

        if (not self._model.hasPassword(user)
                or not self._model._cryptContext.verify(old, user['salt'])):
            # If not the user's actual password, check for temp access token
            token = Token().load(old, force=True, objectId=False, exc=False)
            if (not token or not token.get('userId')
                    or token['userId'] != user['_id']
                    or not Token().hasScope(token, TokenScope.TEMPORARY_USER_AUTH)):
                raise AccessException('Old password is incorrect.')

        self._model.setPassword(user, new)

        if token:
            # Remove the temporary access token if one was used
            Token().remove(token)

        return {'message': 'Password changed.'}

    @access.public
    @autoDescribeRoute(
        Description("Create a temporary access token for a user.  The user's "
                    'password is not changed.')
        .param('email', 'Your email address.', strip=True)
        .errorResponse('That email does not exist in the system.')
    )
    def generateTemporaryPassword(self, email):
        user = self._model.findOne({'email': email.lower()})

        if not user:
            raise RestException('That email is not registered.')

        token = Token().createToken(user, days=1, scope=TokenScope.TEMPORARY_USER_AUTH)

        url = '%s#useraccount/%s/token/%s' % (
            mail_utils.getEmailUrlPrefix(), str(user['_id']), str(token['_id']))

        html = mail_utils.renderTemplate('temporaryAccess.mako', {
            'url': url,
            'token': str(token['_id'])
        })
        mail_utils.sendMail(
            '%s: Temporary access' % Setting().get(SettingKey.BRAND_NAME),
            html,
            [email]
        )
        return {'message': 'Sent temporary access email.'}

    @access.public
    @autoDescribeRoute(
        Description('Check if a specified token is a temporary access token '
                    'for the specified user.  If the token is valid, returns '
                    'information on the token and user.')
        .modelParam('id', 'The user ID to check.', model=UserModel, force=True)
        .param('token', 'The token to check.')
        .errorResponse('The token does not grant temporary access to the specified user.', 401)
    )
    def checkTemporaryPassword(self, user, token):
        token = Token().load(
            token, user=user, level=AccessType.ADMIN, objectId=False, exc=True)
        delta = (token['expires'] - datetime.datetime.utcnow()).total_seconds()
        hasScope = Token().hasScope(token, TokenScope.TEMPORARY_USER_AUTH)

        if token.get('userId') != user['_id'] or delta <= 0 or not hasScope:
            raise AccessException('The token does not grant temporary access to this user.')

        # Temp auth is verified, send an actual auth token now. We keep the
        # temp token around since it can still be used on a subsequent request
        # to change the password
        authToken = self.sendAuthTokenCookie(user)

        return {
            'user': self._model.filter(user, user),
            'authToken': {
                'token': authToken['_id'],
                'expires': authToken['expires'],
                'temporary': True
            },
            'message': 'Temporary access token is valid.'
        }

    @access.public
    @autoDescribeRoute(
        Description('Get detailed information about a user.')
        .modelParam('id', model=UserModel, level=AccessType.READ)
        .errorResponse()
        .errorResponse('Read access was denied on the user.', 403)
    )
    def getUserDetails(self, user):
        return {
            'nFolders': self._model.countFolders(
                user, filterUser=self.getCurrentUser(), level=AccessType.READ)
        }

    @access.user
    @autoDescribeRoute(
        Description('Initiate the enablement of one-time passwords for this user.')
        .modelParam('id', model=UserModel, level=AccessType.ADMIN)
        .errorResponse()
        .errorResponse('Admin access was denied on the user.', 403)
    )
    def initializeOtp(self, user):
        if self._model.hasOtpEnabled(user):
            raise RestException('The user has already enabled one-time passwords.')

        otpUris = self._model.initializeOtp(user)
        self._model.save(user)

        return otpUris

    @access.user
    @autoDescribeRoute(
        Description('Finalize the enablement of one-time passwords for this user.')
        .modelParam('id', model=UserModel, level=AccessType.ADMIN)
        .param('Girder-OTP', 'A one-time password for this user', paramType='header')
        .errorResponse()
        .errorResponse('Admin access was denied on the user.', 403)
    )
    def finalizeOtp(self, user):
        otpToken = cherrypy.request.headers.get('Girder-OTP')
        if not otpToken:
            raise RestException('The "Girder-OTP" header must be provided.')

        if 'otp' not in user:
            raise RestException('The user has not initialized one-time passwords.')
        if self._model.hasOtpEnabled(user):
            raise RestException('The user has already enabled one-time passwords.')

        user['otp']['enabled'] = True
        # This will raise an exception if the verification fails, so the user will not be saved
        self._model.verifyOtp(user, otpToken)

        self._model.save(user)

    @access.user
    @autoDescribeRoute(
        Description('Disable one-time passwords for this user.')
        .modelParam('id', model=UserModel, level=AccessType.ADMIN)
        .errorResponse()
        .errorResponse('Admin access was denied on the user.', 403)
    )
    def removeOtp(self, user):
        if not self._model.hasOtpEnabled(user):
            raise RestException('The user has not enabled one-time passwords.')

        del user['otp']
        self._model.save(user)

    @access.public
    @autoDescribeRoute(
        Description('Verify an email address using a token.')
        .modelParam('id', 'The user ID to check.', model=UserModel, force=True)
        .param('token', 'The token to check.')
        .errorResponse('The token is invalid or expired.', 401)
    )
    def verifyEmail(self, user, token):
        token = Token().load(
            token, user=user, level=AccessType.ADMIN, objectId=False, exc=True)
        delta = (token['expires'] - datetime.datetime.utcnow()).total_seconds()
        hasScope = Token().hasScope(token, TokenScope.EMAIL_VERIFICATION)

        if token.get('userId') != user['_id'] or delta <= 0 or not hasScope:
            raise AccessException('The token is invalid or expired.')

        user['emailVerified'] = True
        Token().remove(token)
        user = self._model.save(user)

        if self._model.canLogin(user):
            setCurrentUser(user)
            authToken = self.sendAuthTokenCookie(user)
            return {
                'user': self._model.filter(user, user),
                'authToken': {
                    'token': authToken['_id'],
                    'expires': authToken['expires'],
                    'scope': authToken['scope']
                },
                'message': 'Email verification succeeded.'
            }
        else:
            return {
                'user': self._model.filter(user, user),
                'message': 'Email verification succeeded.'
            }

    @access.public
    @autoDescribeRoute(
        Description('Send verification email.')
        .param('login', 'Your login or email address.', strip=True)
        .errorResponse('That login is not registered.', 401)
    )
    def sendVerificationEmail(self, login):
        loginField = 'email' if '@' in login else 'login'
        user = self._model.findOne({loginField: login.lower()})

        if not user:
            raise RestException('That login is not registered.', 401)

        self._model._sendVerificationEmail(user)
        return {'message': 'Sent verification email.'}