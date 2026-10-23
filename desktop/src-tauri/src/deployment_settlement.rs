#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum Status {
    Pending,
    Terminal,
    Unobservable,
}

pub(crate) trait Operation {
    type Output;
    fn status(&mut self) -> Status;
    fn cancel(&mut self);
    fn results(&mut self) -> Self::Output;
}

// The caller retains its operation and apartment until an observed terminal state.
// The cancellation budget bounds the request time, not settlement time.
pub(crate) fn wait<O: Operation>(
    operation: &mut O,
    mut cancellation_requested: impl FnMut() -> bool,
    mut pause: impl FnMut(),
) -> O::Output {
    let mut cancel_requested = false;
    loop {
        if operation.status() == Status::Terminal {
            return operation.results();
        }
        if !cancel_requested && cancellation_requested() {
            cancel_requested = true;
            operation.cancel();
        }
        pause();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::Cell, collections::VecDeque, rc::Rc};

    struct Injected {
        statuses: VecDeque<Status>,
        cancels: usize,
        polls: usize,
        result: Option<Result<&'static str, i32>>,
        alive: Rc<Cell<bool>>,
    }
    impl Drop for Injected {
        fn drop(&mut self) {
            self.alive.set(false);
        }
    }
    impl Operation for Injected {
        type Output = Result<&'static str, i32>;
        fn status(&mut self) -> Status {
            self.polls += 1;
            self.statuses.pop_front().expect("unexpected poll")
        }
        fn cancel(&mut self) {
            self.cancels += 1;
        }
        fn results(&mut self) -> Self::Output {
            assert!(self.statuses.is_empty(), "results before terminal");
            self.result.take().expect("results called once")
        }
    }
    fn injected(statuses: &[Status], result: Result<&'static str, i32>) -> Injected {
        Injected {
            statuses: statuses.iter().copied().collect(),
            cancels: 0,
            polls: 0,
            result: Some(result),
            alive: Rc::new(Cell::new(true)),
        }
    }
    #[test]
    fn cancel_then_started_retains_same_operation_until_terminal() {
        let mut op = injected(
            &[Status::Pending, Status::Pending, Status::Terminal],
            Ok("actual result"),
        );
        let alive = op.alive.clone();
        let mut pauses = 0;
        assert_eq!(
            wait(
                &mut op,
                || true,
                || {
                    assert!(alive.get());
                    pauses += 1;
                }
            ),
            Ok("actual result")
        );
        assert_eq!((op.cancels, op.polls, pauses), (1, 3, 2));
        assert!(alive.get());
        drop(op);
        assert!(!alive.get());
    }
    #[test]
    fn completed_race_returns_result_without_cancel() {
        let mut op = injected(&[Status::Terminal], Ok("completed"));
        assert_eq!(
            wait(
                &mut op,
                || panic!("terminal wins"),
                || panic!("terminal needs no pause")
            ),
            Ok("completed")
        );
        assert_eq!(op.cancels, 0);
    }
    #[test]
    fn terminal_failure_is_returned_instead_of_fabricated_cancellation() {
        let mut op = injected(&[Status::Pending, Status::Terminal], Err(-12345));
        assert_eq!(wait(&mut op, || true, || {}), Err(-12345));
        assert_eq!(op.cancels, 1);
    }
    #[test]
    fn status_failure_while_pending_keeps_polling_same_operation() {
        let mut op = injected(
            &[
                Status::Pending,
                Status::Unobservable,
                Status::Unobservable,
                Status::Pending,
                Status::Terminal,
            ],
            Ok("settled"),
        );
        let mut requests = 0;
        assert_eq!(
            wait(
                &mut op,
                || {
                    requests += 1;
                    requests == 2
                },
                || {}
            ),
            Ok("settled")
        );
        assert_eq!((op.polls, op.cancels, requests), (5, 1, 2));
    }
    #[test]
    fn prolonged_pending_does_not_retry_cancel_or_return_early() {
        let mut statuses = vec![Status::Unobservable; 1000];
        statuses.push(Status::Terminal);
        let mut op = injected(&statuses, Err(-2147023673));
        assert_eq!(wait(&mut op, || true, || {}), Err(-2147023673));
        assert_eq!((op.cancels, op.polls), (1, 1001));
    }
}
