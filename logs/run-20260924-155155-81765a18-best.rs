#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Version {
    pub major: u64,
    pub minor: u64,
    pub patch: u64,
    pub prerelease: Option<String>,
    pub build: Option<String>,
}

impl Version {
    pub fn parse(text: &str) -> Result<Version, String> {
        let mut prerelease: Option<String> = None;
        let mut build: Option<String> = None;

        let mut remaining = text;

        // Parse build metadata
        if let Some(plus_idx) = remaining.find('+') {
            let build_str = &remaining[plus_idx + 1..];
            if build_str.is_empty() {
                return Err("Build metadata cannot be empty".to_string());
            }
            for part in build_str.split('.') {
                if part.is_empty() {
                    return Err("Build identifier part cannot be empty".to_string());
                }
                if !is_valid_build_identifier(part) {
                    return Err(format!("Invalid build identifier: {}", part));
                }
            }
            build = Some(build_str.to_string());
            remaining = &remaining[..plus_idx];
        }

        // Parse prerelease
        if let Some(hyphen_idx) = remaining.find('-') {
            let prerelease_str = &remaining[hyphen_idx + 1..];
            if prerelease_str.is_empty() {
                return Err("Prerelease cannot be empty".to_string());
            }
            for part in prerelease_str.split('.') {
                if part.is_empty() {
                    return Err("Prerelease identifier part cannot be empty".to_string());
                }
                if part.chars().all(|c| c.is_ascii_digit()) {
                    // Check for leading zeros in numeric prerelease identifiers
                    if part.len() > 1 && part.starts_with('0') {
                        return Err(format!("Numeric prerelease identifier cannot have leading zeros: {}", part));
                    }
                }
                if !is_valid_prerelease_identifier(part) {
                    return Err(format!("Invalid prerelease identifier: {}", part));
                }
            }
            prerelease = Some(prerelease_str.to_string());
            remaining = &remaining[..hyphen_idx];
        }

        let parts: Vec<&str> = remaining.split('.').collect();
        if parts.len() != 3 {
            return Err(format!("Expected 3 parts for major.minor.patch, got {}", parts.len()));
        }

        let major = parse_numeric_part(parts[0])?;
        let minor = parse_numeric_part(parts[1])?;
        let patch = parse_numeric_part(parts[2])?;

        Ok(Version {
            major,
            minor,
            patch,
            prerelease,
            build,
        })
    }

    pub fn to_string(&self) -> String {
        let mut s = format!("{}.{}.{}", self.major, self.minor, self.patch);
        if let Some(prerelease) = &self.prerelease {
            s.push('-');
            s.push_str(prerelease);
        }
        if let Some(build) = &self.build {
            s.push('+');
            s.push_str(build);
        }
        s
    }

    pub fn bump_major(&self) -> Version {
        Version {
            major: self.major + 1,
            minor: 0,
            patch: 0,
            prerelease: None,
            build: None,
        }
    }

    pub fn bump_minor(&self) -> Version {
        Version {
            major: self.major,
            minor: self.minor + 1,
            patch: 0,
            prerelease: None,
            build: None,
        }
    }

    pub fn bump_patch(&self) -> Version {
        Version {
            major: self.major,
            minor: self.minor,
            patch: self.patch + 1,
            prerelease: None,
            build: None,
        }
    }

    pub fn compare(&self, other: &Version) -> std::cmp::Ordering {
        let cmp_major = self.major.cmp(&other.major);
        if cmp_major != std::cmp::Ordering::Equal {
            return cmp_major;
        }

        let cmp_minor = self.minor.cmp(&other.minor);
        if cmp_minor != std::cmp::Ordering::Equal {
            return cmp_minor;
        }

        let cmp_patch = self.patch.cmp(&other.patch);
        if cmp_patch != std::cmp::Ordering::Equal {
            return cmp_patch;
        }

        compare_prerelease(&self.prerelease, &other.prerelease)
    }
}

// Helper functions for parsing
fn parse_numeric_part(s: &str) -> Result<u64, String> {
    if s.is_empty() {
        return Err("Numeric identifier cannot be empty".to_string());
    }
    if s.len() > 1 && s.starts_with('0') {
        return Err(format!("Numeric identifier cannot have leading zeros: {}", s));
    }
    s.parse::<u64>().map_err(|e| format!("Invalid numeric identifier '''{}''': {}", s, e))
}

fn is_valid_identifier_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '-'
}

fn is_valid_prerelease_identifier(s: &str) -> bool {
    !s.is_empty() && s.chars().all(is_valid_identifier_char)
}

fn is_valid_build_identifier(s: &str) -> bool {
    !s.is_empty() && s.chars().all(is_valid_identifier_char)
}

// Helper functions for comparison
fn nat_cmp_identifiers(a: &str, b: &str) -> std::cmp::Ordering {
    let a_is_numeric = a.chars().all(|c| c.is_ascii_digit());
    let b_is_numeric = b.chars().all(|c| c.is_ascii_digit());

    match (a_is_numeric, b_is_numeric) {
        (true, true) => {
            let a_num = a.parse::<u64>().unwrap_or(0);
            let b_num = b.parse::<u64>().unwrap_or(0);
            a_num.cmp(&b_num)
        }
        (false, false) => a.cmp(b),
        (true, false) => std::cmp::Ordering::Less,
        (false, true) => std::cmp::Ordering::Greater,
    }
}

fn compare_prerelease(a: &Option<String>, b: &Option<String>) -> std::cmp::Ordering {
    match (a, b) {
        (None, None) => std::cmp::Ordering::Equal,
        (Some(_), None) => std::cmp::Ordering::Less, // Version with prerelease has lower precedence
        (None, Some(_)) => std::cmp::Ordering::Greater, // Version without prerelease has higher precedence
        (Some(a_str), Some(b_str)) => {
            let mut a_parts = a_str.split('.');
            let mut b_parts = b_str.split('.');

            loop {
                let a_part = a_parts.next();
                let b_part = b_parts.next();

                match (a_part, b_part) {
                    (Some(ap), Some(bp)) => {
                        let cmp = nat_cmp_identifiers(ap, bp);
                        if cmp != std::cmp::Ordering::Equal {
                            return cmp;
                        }
                    }
                    (Some(_), None) => return std::cmp::Ordering::Greater,
                    (None, Some(_)) => return std::cmp::Ordering::Less,
                    (None, None) => return std::cmp::Ordering::Equal,
                }
            }
        }
    }
}

// Public top-level functions (delegate to Version methods)
pub fn parse(text: &str) -> Result<Version, String> {
    Version::parse(text)
}

pub fn to_string(version: &Version) -> String {
    version.to_string()
}

pub fn compare(a: &Version, b: &Version) -> std::cmp::Ordering {
    a.compare(b)
}

pub fn bump_major(version: &Version) -> Version {
    version.bump_major()
}

pub fn bump_minor(version: &Version) -> Version {
    version.bump_minor()
}

pub fn bump_patch(version: &Version) -> Version {
    version.bump_patch()
}

// Unit tests
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse() {
        assert!(parse("1.2.3").is_ok());
        assert!(parse("1.2.3-alpha").is_ok());
        assert!(parse("1.2.3+build.1").is_ok());
        assert!(parse("1.2.3-alpha.1+build.1").is_ok());
        assert!(parse("1.0.0-0A.1").is_err()); // Invalid prerelease char
        assert!(parse("1.0.0-alpha.01").is_err()); // Leading zero in numeric prerelease
        assert!(parse("1.0.0-alpha.").is_err()); // Empty prerelease identifier part
        assert!(parse("1.0.0+").is_err()); // Empty build metadata
        assert!(parse("1.0.0+build.").is_err()); // Empty build identifier part
        assert!(parse("1.2").is_err()); // Not enough parts
        assert!(parse("01.2.3").is_err()); // Leading zero in major
    }

    #[test]
    fn test_to_string() {
        assert_eq!(to_string(&parse("1.2.3").unwrap()), "1.2.3");
        assert_eq!(to_string(&parse("1.2.3-alpha").unwrap()), "1.2.3-alpha");
        assert_eq!(to_string(&parse("1.2.3+build.1").unwrap()), "1.2.3+build.1");
        assert_eq!(to_string(&parse("1.2.3-alpha.1+build.1").unwrap()), "1.2.3-alpha.1+build.1");
    }

    #[test]
    fn test_compare() {
        let v1 = parse("1.0.0").unwrap();
        let v2 = parse("2.0.0").unwrap();
        assert_eq!(compare(&v1, &v2), std::cmp::Ordering::Less);
        assert_eq!(compare(&v2, &v1), std::cmp::Ordering::Greater);
        assert_eq!(compare(&v1, &v1), std::cmp::Ordering::Equal);

        let pa = parse("1.0.0-alpha").unwrap();
        let pa1 = parse("1.0.0-alpha.1").unwrap();
        assert_eq!(compare(&pa, &pa1), std::cmp::Ordering::Less);
        assert_eq!(compare(&pa1, &pa), std::cmp::Ordering::Greater);

        let p_beta = parse("1.0.0-beta.2").unwrap();
        let p_beta11 = parse("1.0.0-beta.11").unwrap();
        assert_eq!(compare(&p_beta, &p_beta11), std::cmp::Ordering::Less);
        assert_eq!(compare(&p_beta11, &p_beta), std::cmp::Ordering::Greater);
    }

    #[test]
    fn test_bump_major() {
        let ver = parse("1.2.3-alpha+build").unwrap();
        let bumped = bump_major(&ver);
        assert_eq!(to_string(&bumped), "2.0.0");
    }

    #[test]
    fn test_bump_minor() {
        let ver = parse("1.2.3-alpha+build").unwrap();
        let bumped = bump_minor(&ver);
        assert_eq!(to_string(&bumped), "1.3.0");
    }

    #[test]
    fn test_bump_patch() {
        let ver = parse("1.2.3-alpha+build").unwrap();
        let bumped = bump_patch(&ver);
        assert_eq!(to_string(&bumped), "1.2.4");
    }
}
