
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
        let cmp_major = self.major.cmp(&other.major) as i8;
        if cmp_major != 0 {
            return cmp_major;
        }

        let cmp_minor = self.minor.cmp(&other.minor) as i8;
        if cmp_minor != 0 {
            return cmp_minor;
        }

        let cmp_patch = self.patch.cmp(&other.patch) as i8;
        if cmp_patch != 0 {
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

fn is_valid_numeric(c: char) -> bool {
    c.is_ascii_digit()
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
fn nat_cmp_identifiers(a: &str, b: &str) -> i8 {
    let a_is_numeric = a.chars().all(|c| c.is_ascii_digit());
    let b_is_numeric = b.chars().all(|c| c.is_ascii_digit());

    match (a_is_numeric, b_is_numeric) {
        (true, true) => {
            let a_num = a.parse::<u64>().unwrap_or(0); // Should not fail if all are digits and valid
            let b_num = b.parse::<u64>().unwrap_or(0); // Should not fail if all are digits and valid
            a_num.cmp(&b_num) as i8
        }
        (false, false) => a.cmp(b) as i8, // Lexical comparison
        (true, false) => -1, // Numeric has lower precedence
        (false, true) => 1,  // Non-numeric has higher precedence
    }
}

fn compare_prerelease(a: &Option<String>, b: &Option<String>) -> i8 {
    match (a, b) {
        (None, None) => 0,
        (Some(_), None) => -1, // Version with prerelease has lower precedence
        (None, Some(_)) => 1,  // Version without prerelease has higher precedence
        (Some(a_str), Some(b_str)) => {
            let mut a_parts = a_str.split('.');
            let mut b_parts = b_str.split('.');

            loop {
                let a_part = a_parts.next();
                let b_part = b_parts.next();

                match (a_part, b_part) {
                    (Some(ap), Some(bp)) => {
                        let cmp = nat_cmp_identifiers(ap, bp);
                        if cmp != 0 {
                            return cmp;
                        }
                    }
                    (Some(_), None) => return 1, // 'a' has more identifiers, so it's greater
                    (None, Some(_)) => return -1, // 'b' has more identifiers, so it's greater
                    (None, None) => return 0, // Both ran out of identifiers, they are equal
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

pub fn compare(version_a: &str, version_b: &str) -> Result<i8, String> {
    let v_a = Version::parse(version_a)?;
    let v_b = Version::parse(version_b)?;
    Ok(v_a.compare(&v_b))
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
        assert_eq!(compare("1.0.0", "2.0.0").unwrap(), -1);
        assert_eq!(compare("2.0.0", "1.0.0").unwrap(), 1);
        assert_eq!(compare("2.0.0", "2.0.0").unwrap(), 0);
        assert_eq!(compare("1.0.0-alpha", "1.0.0-alpha.1").unwrap(), -1);
        assert_eq!(compare("1.0.0-alpha.1", "1.0.0-alpha").unwrap(), 1);
        assert_eq!(compare("1.0.0-alpha.beta", "1.0.0-alpha.1").unwrap(), 1);
        assert_eq!(compare("1.0.0-beta", "1.0.0-alpha.beta").unwrap(), 1);
        assert_eq!(compare("1.0.0-beta.2", "1.0.0-beta.11").unwrap(), -1);
        assert_eq!(compare("1.0.0-rc.1", "1.0.0-beta.11").unwrap(), 1);
        assert_eq!(compare("1.0.0", "1.0.0-rc.1").unwrap(), 1); // No prerelease > with prerelease
        assert_eq!(compare("1.0.0-rc.1", "1.0.0").unwrap(), -1); // With prerelease < no prerelease
        assert_eq!(compare("1.0.0+build.1", "1.0.0+build.2").unwrap(), 0); // Build metadata ignored
        assert_eq!(compare("1.0.0-alpha+build.1", "1.0.0-alpha+build.2").unwrap(), 0); // Build metadata ignored
        assert_eq!(compare("1.0.0-alpha.1", "1.0.0-alpha.beta").unwrap(), -1); // alpha.1 < alpha.beta (numeric vs alpha)
        assert_eq!(compare("1.0.0-beta.11", "1.0.0-beta.2").unwrap(), 1); // beta.11 > beta.2 (numeric comparison)
        assert_eq!(compare("1.0.0-rc.1", "1.0.0-rc.1").unwrap(), 0); // equal prerelease
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
