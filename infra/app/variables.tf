variable "region" {
  description = "AWS region (same as the bootstrap stack)."
  type        = string
  default     = "eu-west-2" # London
}

variable "project" {
  description = "Short name used in resource names and the Project tag (same as the bootstrap stack)."
  type        = string
  default     = "places"
}

variable "instance_type" {
  description = "The app server. x86, because the PostGIS image has no ARM build."
  type        = string
  default     = "t3a.small" # 2 vCPU, 2 GiB
}

variable "root_volume_gb" {
  description = "OS, Docker images and logs. Replaceable: nothing here needs keeping."
  type        = number
  default     = 16
}

variable "data_volume_gb" {
  description = "The separate volume that holds the database and local backups."
  type        = number
  default     = 8
}

variable "sender_email" {
  description = "Address magic-link emails are sent from. SES emails it a verification link on apply. Set it in terraform.tfvars (gitignored)."
  type        = string
}

variable "osm_contact" {
  description = "URL or email OpenStreetMap services can reach you at (sent in the User-Agent; their usage policies require it)."
  type        = string
}

variable "backup_retention_days" {
  description = "How long nightly database dumps are kept in S3."
  type        = number
  default     = 30
}
