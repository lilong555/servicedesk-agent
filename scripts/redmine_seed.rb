require "json"
require "securerandom"

raise "This seed requires the isolated DESK_DEMO container." unless ENV["DESK_DEMO"] == "1"

Redmine::DefaultData::Loader.load("en") if IssueStatus.count.zero?
Setting.rest_api_enabled = "1"
Setting.notified_events = []
Setting.self_registration = "0"

admin = User.find_by!(login: "admin")
admin_password = SecureRandom.hex(20)
admin.password = admin_password
admin.password_confirmation = admin_password
admin.must_change_passwd = false
admin.save!
if ENV["DESK_ROTATE_KEYS"] == "1"
  admin.api_token&.destroy!
  admin.reload
end
User.current = admin

project = Project.find_or_initialize_by(identifier: "servicedesk-demo")
project.name = "ServiceDesk Agent Demo"
project.is_public = false
project.enabled_module_names = ["issue_tracking"]
project.trackers = Tracker.all
project.save!

role = Role.find_or_initialize_by(name: "ServiceDesk Agent")
role.permissions = [:view_issues, :add_issues, :edit_issues, :add_issue_notes]
role.save!

users = {}
passwords = {}
[
  ["desk.agent", "Desk", "Agent"],
  ["engineer.one", "Engineer", "One"],
  ["engineer.two", "Engineer", "Two"]
].each do |login, first, last|
  user = User.find_or_initialize_by(login: login)
  password = SecureRandom.hex(20)
  user.firstname = first
  user.lastname = last
  user.mail = "#{login}@example.invalid"
  user.password = password
  user.password_confirmation = password
  user.must_change_passwd = false
  user.status = User::STATUS_ACTIVE
  user.save!
  if ENV["DESK_ROTATE_KEYS"] == "1"
    user.api_token&.destroy!
    user.reload
  end
  member = Member.find_or_initialize_by(project: project, user: user)
  member.roles = [role]
  member.save!
  users[login] = user
  passwords[login] = password
end

Tracker.all.each do |tracker|
  IssueStatus.all.each do |old_status|
    IssueStatus.all.each do |new_status|
      next if old_status == new_status
      WorkflowTransition.where(
        role_id: role.id, tracker_id: tracker.id,
        old_status_id: old_status.id, new_status_id: new_status.id
      ).first_or_create!
    end
  end
end

issue = Issue.find_or_initialize_by(project: project, subject: "VPN cross-session investigation")
if issue.new_record?
  issue.tracker = Tracker.find_by(name: "Support") || Tracker.first!
  issue.author = admin
  issue.assigned_to = users["engineer.one"]
  issue.status = IssueStatus.find_by(is_closed: false)
  issue.priority = IssuePriority.default || IssuePriority.first!
  issue.description = "Investigate intermittent VPN disconnects, add notes, and keep the ticket open."
  issue.save!
end

puts "DESK_SEED_JSON=" + JSON.generate({
  project_id: project.id,
  ticket_id: issue.id,
  agent_id: users["desk.agent"].id,
  agent_api_key: users["desk.agent"].api_key,
  agent_password: passwords["desk.agent"],
  admin_api_key: admin.api_key,
  admin_password: admin_password,
  first_owner_id: users["engineer.one"].id,
  second_owner_id: users["engineer.two"].id
})
