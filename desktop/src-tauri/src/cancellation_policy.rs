use std::time::Duration;

#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum Status {
    Started,
    Completed,
    Canceled,
    Error,
    Unobservable,
}
#[derive(Debug, PartialEq)]
pub(crate) enum Step {
    Poll,
    Cancel,
    Unresolved,
    Terminal(Status),
}
#[derive(Default)]
pub(crate) struct Wait {
    pub(crate) cancel_at: Option<Duration>,
    reported: bool,
}
impl Wait {
    pub(crate) fn observe(&mut self, status: Status, elapsed: Duration, request: bool) -> Step {
        match status {
            Status::Completed | Status::Canceled | Status::Error => return Step::Terminal(status),
            _ => {}
        }
        if status == Status::Started && request && self.cancel_at.is_none() {
            self.cancel_at = Some(elapsed);
            return Step::Cancel;
        }
        if self
            .cancel_at
            .is_some_and(|at| elapsed.saturating_sub(at) >= Duration::from_secs(10))
            && !self.reported
        {
            self.reported = true;
            return Step::Unresolved;
        }
        Step::Poll
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn seconds(n: u64) -> Duration {
        Duration::from_secs(n)
    }
    #[test]
    fn unseen_status_never_proves_cancel() {
        let mut wait = Wait::default();
        assert_eq!(
            wait.observe(Status::Unobservable, seconds(100), true),
            Step::Poll
        );
        assert_eq!(wait.cancel_at, None);
        assert_eq!(
            wait.observe(Status::Started, seconds(101), true),
            Step::Cancel
        );
    }
    #[test]
    fn cancel_once_and_retain_pending() {
        let mut wait = Wait::default();
        assert_eq!(
            wait.observe(Status::Started, seconds(0), true),
            Step::Cancel
        );
        assert_eq!(wait.observe(Status::Started, seconds(9), true), Step::Poll);
        assert_eq!(
            wait.observe(Status::Unobservable, seconds(10), true),
            Step::Unresolved
        );
        assert_eq!(
            wait.observe(Status::Started, seconds(1000), true),
            Step::Poll
        );
    }
    #[test]
    fn terminal_races_win() {
        for status in [Status::Completed, Status::Canceled, Status::Error] {
            let mut wait = Wait::default();
            assert_eq!(
                wait.observe(status, seconds(0), true),
                Step::Terminal(status)
            );
            wait.observe(Status::Started, seconds(1), true);
            assert_eq!(
                wait.observe(status, seconds(11), true),
                Step::Terminal(status)
            );
        }
    }
}
